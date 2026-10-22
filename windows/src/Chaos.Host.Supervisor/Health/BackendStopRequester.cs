using System.Globalization;
using System.Net;
using System.Security.Cryptography;

namespace Chaos.Host.Supervisor.Health;

/// <summary>
/// The contract between the supervisor and the platform's loopback stop
/// endpoint (<c>src/chaos/api</c>, <c>POST /_supervisor/stop</c>).
/// </summary>
/// <remarks>
/// <para>
/// Windows has no <c>SIGTERM</c>, and a service's child has no console to
/// receive a control event, so the one in-band "please stop" that works in
/// every run mode is an HTTP request the backend itself honours. The endpoint
/// is unlisted and lies outside every proxied prefix (the route-ownership
/// manifest never claims <c>/_supervisor</c>), it answers 404 unless
/// <see cref="SecretEnvironmentVariable"/> was set in the child's environment,
/// it requires the header <see cref="SecretHeader"/> to equal that value and
/// the peer to be loopback, and it answers <c>202 {"stopping": true}</c> before
/// shutting uvicorn down so the application's lifespan cleanup runs.
/// </para>
/// <para>
/// The secret is generated per launch by the supervisor and never logged. It
/// is not a setting: an operator value in <c>homestead.env</c> is overridden
/// by the supervisor's own, because only the supervisor knows what it will
/// send.
/// </para>
/// </remarks>
public static class BackendStopContract
{
    /// <summary>The endpoint's path on the backend.</summary>
    public const string Path = "/_supervisor/stop";

    /// <summary>The environment variable that carries the secret into the child.</summary>
    public const string SecretEnvironmentVariable = "CHAOS_SUPERVISOR_STOP_SECRET";

    /// <summary>The request header that must carry the secret back.</summary>
    public const string SecretHeader = "X-Chaos-Supervisor-Secret";

    /// <summary>The only status that means the backend took the request.</summary>
    public const HttpStatusCode AcceptedStatus = HttpStatusCode.Accepted;

    /// <summary>A fresh secret: 32 random bytes as 64 hexadecimal characters.</summary>
    public static string NewSecret() => Convert.ToHexString(RandomNumberGenerator.GetBytes(32));
}

/// <summary>What one <c>POST /_supervisor/stop</c> told us.</summary>
/// <param name="Accepted">True only when the backend answered <c>202</c>.</param>
/// <param name="Detail">Why, in words an operator can act on. Never contains the secret.</param>
public readonly record struct BackendStopResult(bool Accepted, string Detail);

/// <summary>Asks the backend, over loopback HTTP, to shut itself down.</summary>
public interface IBackendStopRequester
{
    /// <summary>
    /// POSTs the stop request. Returns a verdict rather than throwing: "no
    /// endpoint", "refused" and "unreachable" are all reasons to fall back to
    /// the next stop path, not reasons to abandon the stop.
    /// </summary>
    Task<BackendStopResult> RequestStopAsync(Uri endpoint, string secret, TimeSpan timeout, CancellationToken cancellationToken);
}

/// <summary>
/// The HTTP implementation, built the same way as
/// <see cref="HttpBackendHealthProbe"/>: one long-lived client against one
/// fixed loopback endpoint.
/// </summary>
public sealed class HttpBackendStopRequester : IBackendStopRequester, IDisposable
{
    private readonly HttpClient _client;
    private readonly bool _ownsClient;
    private bool _disposed;

    public HttpBackendStopRequester()
    {
        _client = new HttpClient(new SocketsHttpHandler
        {
            ConnectTimeout = TimeSpan.FromSeconds(5),
            PooledConnectionLifetime = TimeSpan.FromMinutes(5),
            AllowAutoRedirect = false,
        })
        {
            // Cancellation is per call, from the caller's timeout.
            Timeout = System.Threading.Timeout.InfiniteTimeSpan,
        };
        _ownsClient = true;
    }

    public HttpBackendStopRequester(HttpClient client)
    {
        _client = client ?? throw new ArgumentNullException(nameof(client));
        _ownsClient = false;
    }

    public async Task<BackendStopResult> RequestStopAsync(Uri endpoint, string secret, TimeSpan timeout, CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(endpoint);
        ArgumentException.ThrowIfNullOrEmpty(secret);

        using var timeoutSource = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        if (timeout > TimeSpan.Zero)
        {
            timeoutSource.CancelAfter(timeout);
        }

        try
        {
            using var request = new HttpRequestMessage(HttpMethod.Post, endpoint);
            request.Headers.TryAddWithoutValidation(BackendStopContract.SecretHeader, secret);

            using var response = await _client
                .SendAsync(request, HttpCompletionOption.ResponseContentRead, timeoutSource.Token)
                .ConfigureAwait(false);

            var status = ((int)response.StatusCode).ToString(CultureInfo.InvariantCulture);
            return response.StatusCode switch
            {
                BackendStopContract.AcceptedStatus => new BackendStopResult(
                    true,
                    $"{endpoint} accepted the stop request (HTTP 202)"),
                HttpStatusCode.NotFound => new BackendStopResult(
                    false,
                    $"{endpoint} answered HTTP 404: this backend has no stop endpoint (a platform older than " +
                    "the contract, or one started without the supervisor's secret in its environment)"),
                HttpStatusCode.Forbidden or HttpStatusCode.Unauthorized => new BackendStopResult(
                    false,
                    $"{endpoint} refused the stop request (HTTP {status}): the secret did not match"),
                _ => new BackendStopResult(
                    false,
                    $"{endpoint} answered HTTP {status} {response.ReasonPhrase}"),
            };
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            throw;
        }
        catch (OperationCanceledException)
        {
            return new BackendStopResult(
                false,
                $"{endpoint} did not answer the stop request within " +
                $"{timeout.TotalSeconds.ToString("0.#", CultureInfo.InvariantCulture)}s");
        }
        catch (HttpRequestException ex)
        {
            return new BackendStopResult(false, $"{endpoint} unreachable: {ex.Message}");
        }
    }

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }

        _disposed = true;
        if (_ownsClient)
        {
            _client.Dispose();
        }
    }
}
