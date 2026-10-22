using Chaos.Host.Supervisor.Events;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;

namespace Chaos.Host.Supervisor;

/// <summary>How the process's exit code is reported when the host stops itself.</summary>
/// <remarks>
/// The supervisor knows it wants the process to exit non-zero; it does not
/// know whether it is a Windows service, where the code also has to be
/// handed to the service control manager. The gateway registers an
/// implementation that does both; this library's default sets
/// <see cref="Environment.ExitCode"/> only.
/// </remarks>
public interface IHostExitCodeReporter
{
    /// <summary>Records <paramref name="exitCode"/> as the code the process will exit with.</summary>
    void Report(int exitCode);
}

/// <summary>Sets <see cref="Environment.ExitCode"/>, which <c>Main</c> returns.</summary>
public sealed class EnvironmentExitCodeReporter : IHostExitCodeReporter
{
    /// <inheritdoc />
    public void Report(int exitCode) => Environment.ExitCode = exitCode;
}

/// <summary>
/// Stops the host with a non-zero exit code when the backend supervisor
/// gives up, so a crash-looping backend is visible to whatever runs the host
/// — the service control manager on Windows — rather than hidden behind a
/// gateway that keeps answering 503.
/// </summary>
/// <remarks>
/// <para>
/// A tripped circuit breaker used to be terminal and silent: the supervisor
/// reported <c>Failed</c>, the process stayed alive, and SCM — whose recovery
/// policy is the one thing left that can restart an unattended machine's
/// control plane — saw a service that was running fine. Now the breaker
/// trip writes a critical entry, reports the exit code, and asks the host to
/// stop. The host's own shutdown then runs in full: in-flight requests drain,
/// the supervisor's stop runs (the child is already dead), and <c>Main</c>
/// returns the code.
/// </para>
/// <para>
/// This is a stop with an error, not a crash. SCM applies recovery to it
/// only when the service's failure flag is set (<c>sc failureflag ChaosHost 1</c>,
/// which <c>--install-service</c> does); the MSI's recovery policy on its own
/// covers crashes only. docs/advanced-command-line.md, "The gateway's own
/// switches", says so where an operator will read it.
/// </para>
/// </remarks>
public sealed class SupervisionFailureHostStop : IHostedService
{
    private readonly BackendSupervisor _supervisor;
    private readonly IHostApplicationLifetime _lifetime;
    private readonly BackendSupervisorOptions _options;
    private readonly IHostExitCodeReporter _exitCode;
    private readonly IChaosEventSink _events;
    private readonly ILogger<SupervisionFailureHostStop> _logger;
    private int _stopped;

    public SupervisionFailureHostStop(
        BackendSupervisor supervisor,
        IHostApplicationLifetime lifetime,
        IOptions<BackendSupervisorOptions> options,
        IHostExitCodeReporter exitCode,
        IChaosEventSink events,
        ILogger<SupervisionFailureHostStop> logger)
        : this(supervisor, lifetime, (options ?? throw new ArgumentNullException(nameof(options))).Value, exitCode, events, logger)
    {
    }

    public SupervisionFailureHostStop(
        BackendSupervisor supervisor,
        IHostApplicationLifetime lifetime,
        BackendSupervisorOptions options,
        IHostExitCodeReporter exitCode,
        IChaosEventSink events,
        ILogger<SupervisionFailureHostStop> logger)
    {
        _supervisor = supervisor ?? throw new ArgumentNullException(nameof(supervisor));
        _lifetime = lifetime ?? throw new ArgumentNullException(nameof(lifetime));
        _options = options ?? throw new ArgumentNullException(nameof(options));
        _exitCode = exitCode ?? throw new ArgumentNullException(nameof(exitCode));
        _events = events ?? throw new ArgumentNullException(nameof(events));
        _logger = logger ?? throw new ArgumentNullException(nameof(logger));
    }

    /// <summary>Whether this instance has asked the host to stop.</summary>
    public bool StopRequested => Volatile.Read(ref _stopped) == 1;

    public Task StartAsync(CancellationToken cancellationToken)
    {
        _supervisor.SupervisionFailed += OnSupervisionFailed;
        return Task.CompletedTask;
    }

    public Task StopAsync(CancellationToken cancellationToken)
    {
        _supervisor.SupervisionFailed -= OnSupervisionFailed;
        return Task.CompletedTask;
    }

    private void OnSupervisionFailed(object? sender, BackendSnapshot snapshot)
    {
        if (!_options.StopHostOnCircuitBreakerTrip)
        {
            _logger.LogCritical(
                "Backend supervision failed terminally and {Option} is off: the host stays up with no backend. " +
                "Reason: {Reason}",
                nameof(BackendSupervisorOptions.StopHostOnCircuitBreakerTrip),
                snapshot.FailureReason ?? snapshot.StatusDetail);
            return;
        }

        if (Interlocked.Exchange(ref _stopped, 1) == 1)
        {
            return;
        }

        var code = _options.CircuitBreakerExitCode;
        var message =
            $"Stopping the Project CHAOS host with exit code {code} because backend supervision failed " +
            $"terminally: {snapshot.FailureReason ?? snapshot.StatusDetail} " +
            "The service manager's recovery policy decides whether it comes back.";

        _logger.LogCritical("{Message}", message);
        _events.Write(ChaosEventId.HostStoppingAfterSupervisionFailure, ChaosEventLevel.Error, message);

        _exitCode.Report(code);
        _lifetime.StopApplication();
    }
}
