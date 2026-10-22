using System.Net;
using Chaos.Host.Abstractions;
using Chaos.Host.Supervisor.Events;
using Chaos.Host.Supervisor.Health;
using Chaos.Host.Supervisor.Policy;
using Chaos.Host.Supervisor.Processes;
using Chaos.Host.Supervisor.Tests.Fakes;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Logging.Abstractions;
using Xunit;

namespace Chaos.Host.Supervisor.Tests;

/// <summary>
/// The supervision loop against a fake process, a fake probe and virtual time.
/// </summary>
public sealed class BackendSupervisorTests
{
    private static BackendSupervisorOptions Options() => new()
    {
        Port = 8081,
        InitialRestartDelay = TimeSpan.FromSeconds(1),
        MaxRestartDelay = TimeSpan.FromSeconds(4),
        BackoffJitterFraction = 0.0,
        MaxStartFailures = 3,
        FailureWindow = TimeSpan.FromMinutes(10),
        ReadinessTimeout = TimeSpan.FromSeconds(30),
        HealthPollInterval = TimeSpan.FromSeconds(1),
        GracefulShutdownTimeout = TimeSpan.FromSeconds(5),

        // Every delay is virtual (FakeClock.AutoAdvance), including the startup
        // gate, which would therefore expire before the first probe ran. The
        // tests wait on the state instead, which is what the gateway's
        // hosted service effectively does too.
        WaitForReadyOnStart = false,
    };

    [Fact]
    public async Task A_backend_that_answers_health_is_running_in_both_views()
    {
        var launcher = new FakeProcessLauncher();
        var probe = new FakeHealthProbe();
        probe.SetHealthy();
        var clock = new FakeClock { AutoAdvance = true };
        var events = new RecordingEventSink();

        await using var supervisor = Build(Options(), launcher, probe, clock, events);

        await supervisor.StartAsync(CancellationToken.None);
        await Eventually.IsTrueAsync(() => supervisor.State == SupervisionState.Running, "the fake /health to be seen");

        Assert.Equal(BackendState.Running, supervisor.Status.State);
        Assert.Equal(launcher.Latest.Id, supervisor.Status.ProcessId);
        Assert.True(events.Contains(ChaosEventId.BackendReady));

        var spec = launcher.Latest.Spec;
        Assert.Equal("/usr/bin/python3", spec.FileName);
        Assert.Equal(["-m", "chaos.cli", "--log-level", "INFO", "serve", "--host", "127.0.0.1", "--port", "8081"], spec.Arguments);
        Assert.Equal("1", spec.Environment["PYTHONUNBUFFERED"]);
    }

    // -- The loopback stop endpoint ------------------------------------------

    [Fact]
    public async Task Every_launch_gets_its_own_stop_secret_and_an_operator_value_cannot_replace_it()
    {
        var options = Options();
        options.Environment[BackendStopContract.SecretEnvironmentVariable] = "operator-typed-this";

        var launcher = new FakeProcessLauncher();
        var probe = new FakeHealthProbe();
        probe.SetHealthy();
        var clock = new FakeClock { AutoAdvance = true };

        await using var supervisor = Build(options, launcher, probe, clock, new RecordingEventSink());
        await supervisor.StartAsync(CancellationToken.None);
        await Eventually.IsTrueAsync(() => supervisor.State == SupervisionState.Running, "the backend to come up");

        var first = launcher.Latest.Spec.Environment[BackendStopContract.SecretEnvironmentVariable];
        Assert.NotNull(first);
        Assert.Equal(64, first!.Length);
        Assert.All(first, c => Assert.True(Uri.IsHexDigit(c), $"'{c}' is not a hex digit"));
        Assert.NotEqual("operator-typed-this", first);

        // A restart is a new launch and a new secret.
        launcher.Latest.Exit(1);
        await Eventually.IsTrueAsync(() => launcher.StartCount == 2, "a second launch after the crash");

        var second = launcher.Latest.Spec.Environment[BackendStopContract.SecretEnvironmentVariable];
        Assert.NotNull(second);
        Assert.NotEqual(first, second);
    }

    [Fact]
    public async Task A_backend_that_accepts_the_stop_request_is_neither_signalled_nor_killed()
    {
        var launcher = new FakeProcessLauncher();
        var probe = new FakeHealthProbe();
        probe.SetHealthy();
        var clock = new FakeClock { AutoAdvance = true };
        var events = new RecordingEventSink();

        // The real HTTP requester over a scripted handler: 202, and the
        // "backend" exits on its own as uvicorn would after its lifespan
        // cleanup.
        var handler = new FakeHttpHandler((_, _) =>
        {
            launcher.Latest.Exit(0);
            return Task.FromResult(new HttpResponseMessage(HttpStatusCode.Accepted)
            {
                Content = new StringContent("{\"stopping\": true}"),
            });
        });
        using var requester = new HttpBackendStopRequester(new HttpClient(handler));

        await using var supervisor = Build(Options(), launcher, probe, clock, events, requester);
        await supervisor.StartAsync(CancellationToken.None);
        await Eventually.IsTrueAsync(() => supervisor.State == SupervisionState.Running, "the backend to come up");

        await supervisor.StopAsync(CancellationToken.None);

        var request = Assert.Single(handler.Requests);
        Assert.Equal(HttpMethod.Post, request.Method);
        Assert.Equal(new Uri("http://127.0.0.1:8081/_supervisor/stop"), request.Uri);
        Assert.Equal(launcher.Latest.Spec.Environment[BackendStopContract.SecretEnvironmentVariable], request.Secret);

        Assert.False(launcher.Latest.WasAskedToStop, "the signal path must not run when the endpoint took the request");
        Assert.False(launcher.Latest.WasKilled);
        Assert.True(launcher.Latest.HasExited);
        Assert.False(events.Contains(ChaosEventId.ForcedTermination));
        Assert.Equal(SupervisionState.Stopped, supervisor.State);
        Assert.Contains("loopback stop endpoint", supervisor.Snapshot.LastExitDetail, StringComparison.Ordinal);
        Assert.Equal(0, supervisor.Snapshot.LastExitCode);
    }

    [Fact]
    public async Task A_backend_without_the_endpoint_is_stopped_by_the_signal_path_instead()
    {
        var launcher = new FakeProcessLauncher();
        var probe = new FakeHealthProbe();
        probe.SetHealthy();
        var clock = new FakeClock { AutoAdvance = true };
        var events = new RecordingEventSink();

        var handler = new FakeHttpHandler((_, _) =>
            Task.FromResult(new HttpResponseMessage(HttpStatusCode.NotFound)));
        using var requester = new HttpBackendStopRequester(new HttpClient(handler));

        await using var supervisor = Build(Options(), launcher, probe, clock, events, requester);
        await supervisor.StartAsync(CancellationToken.None);
        await Eventually.IsTrueAsync(() => supervisor.State == SupervisionState.Running, "the backend to come up");

        await supervisor.StopAsync(CancellationToken.None);

        Assert.Single(handler.Requests);
        Assert.True(launcher.Latest.WasAskedToStop, "a 404 falls through to the signal");
        Assert.False(launcher.Latest.WasKilled);
        Assert.False(events.Contains(ChaosEventId.ForcedTermination));
        Assert.Equal(SupervisionState.Stopped, supervisor.State);
        Assert.Contains("process signal", supervisor.Snapshot.LastExitDetail, StringComparison.Ordinal);
    }

    [Fact]
    public async Task A_stop_request_that_hangs_is_abandoned_at_the_timeout_and_the_signal_path_runs()
    {
        var options = Options();
        options.StopRequestTimeout = TimeSpan.FromMilliseconds(200);

        var launcher = new FakeProcessLauncher();
        var probe = new FakeHealthProbe();
        probe.SetHealthy();
        var clock = new FakeClock { AutoAdvance = true };

        // Never answers; the requester's own timeout (real time, bounded) is
        // what ends it.
        var handler = new FakeHttpHandler(async (_, cancellationToken) =>
        {
            await Task.Delay(Timeout.InfiniteTimeSpan, cancellationToken);
            return new HttpResponseMessage(HttpStatusCode.Accepted);
        });
        using var requester = new HttpBackendStopRequester(new HttpClient(handler));

        await using var supervisor = Build(options, launcher, probe, clock, new RecordingEventSink(), requester);
        await supervisor.StartAsync(CancellationToken.None);
        await Eventually.IsTrueAsync(() => supervisor.State == SupervisionState.Running, "the backend to come up");

        await supervisor.StopAsync(CancellationToken.None);

        Assert.True(launcher.Latest.WasAskedToStop);
        Assert.False(launcher.Latest.WasKilled);
        Assert.Equal(SupervisionState.Stopped, supervisor.State);
    }

    [Fact]
    public async Task A_backend_that_takes_the_request_but_never_exits_is_still_killed()
    {
        var launcher = new FakeProcessLauncher();
        var probe = new FakeHealthProbe();
        probe.SetHealthy();
        var clock = new FakeClock { AutoAdvance = true };
        var events = new RecordingEventSink();
        var requester = new FakeStopRequester { Answer = new BackendStopResult(true, "fake: accepted") };

        await using var supervisor = Build(Options(), launcher, probe, clock, events, requester);
        await supervisor.StartAsync(CancellationToken.None);
        await Eventually.IsTrueAsync(() => supervisor.State == SupervisionState.Running, "the backend to come up");

        await supervisor.StopAsync(CancellationToken.None);

        // 202 is a promise, not an exit. The bounded wait still ends in the
        // Job Object, and the forced termination is recorded as one.
        Assert.Single(requester.Requests);
        Assert.True(launcher.Latest.WasKilled);
        Assert.True(events.Contains(ChaosEventId.ForcedTermination));
        Assert.Contains("asked via the loopback stop endpoint", events.Events.Single(e => e.Id == ChaosEventId.ForcedTermination).Message, StringComparison.Ordinal);
    }

    [Fact]
    public async Task The_stop_secret_never_reaches_the_log()
    {
        var launcher = new FakeProcessLauncher();
        var probe = new FakeHealthProbe();
        probe.SetHealthy();
        var clock = new FakeClock { AutoAdvance = true };
        var requester = new FakeStopRequester();
        var capture = new CapturingLoggerProvider();
        using var loggerFactory = LoggerFactory.Create(logging => logging.AddProvider(capture));

        await using var supervisor = new BackendSupervisor(
            Options(),
            new FakeRuntimeResolver(),
            launcher,
            probe,
            requester,
            clock,
            new RecordingEventSink(),
            loggerFactory.CreateLogger<BackendSupervisor>(),
            NoJitterSource.Instance);

        await supervisor.StartAsync(CancellationToken.None);
        await Eventually.IsTrueAsync(() => supervisor.State == SupervisionState.Running, "the backend to come up");
        await supervisor.StopAsync(CancellationToken.None);

        var secret = Assert.Single(requester.Requests).Secret;
        Assert.Equal(launcher.Latest.Spec.Environment[BackendStopContract.SecretEnvironmentVariable], secret);
        Assert.NotEmpty(capture.Lines);
        Assert.DoesNotContain(capture.Lines, line => line.Contains(secret, StringComparison.Ordinal));
        Assert.DoesNotContain(supervisor.Snapshot.StatusDetail, secret, StringComparison.Ordinal);
    }

    [Fact]
    public async Task Stop_asks_the_child_to_exit_and_reports_stopped()
    {
        var launcher = new FakeProcessLauncher();
        var probe = new FakeHealthProbe();
        probe.SetHealthy();
        var clock = new FakeClock { AutoAdvance = true };

        await using var supervisor = Build(Options(), launcher, probe, clock, new RecordingEventSink());
        await supervisor.StartAsync(CancellationToken.None);
        await Eventually.IsTrueAsync(() => supervisor.State == SupervisionState.Running, "the backend to come up");

        await supervisor.StopAsync(CancellationToken.None);

        Assert.True(launcher.Latest.WasAskedToStop);
        Assert.True(launcher.Latest.HasExited);
        Assert.Equal(SupervisionState.Stopped, supervisor.State);
        Assert.Equal(BackendState.Stopped, supervisor.Status.State);
    }

    [Fact]
    public async Task Start_is_idempotent()
    {
        var launcher = new FakeProcessLauncher();
        var probe = new FakeHealthProbe();
        probe.SetHealthy();
        var clock = new FakeClock { AutoAdvance = true };

        await using var supervisor = Build(Options(), launcher, probe, clock, new RecordingEventSink());
        await supervisor.StartAsync(CancellationToken.None);
        await supervisor.StartAsync(CancellationToken.None);
        await Eventually.IsTrueAsync(() => supervisor.State == SupervisionState.Running, "the backend to come up");

        Assert.Equal(1, launcher.StartCount);
    }

    [Fact]
    public async Task A_tripped_breaker_stops_the_host_with_a_non_zero_exit_code()
    {
        var options = Options();
        options.MaxStartFailures = 2;

        // Every launch dies at once: the backend cannot start at all.
        var launcher = new FakeProcessLauncher { Configure = process => process.Exit(1) };
        var probe = new FakeHealthProbe();
        var clock = new FakeClock { AutoAdvance = true };
        var events = new RecordingEventSink();
        var lifetime = new FakeHostApplicationLifetime();
        var exitCode = new RecordingExitCodeReporter();

        await using var supervisor = Build(options, launcher, probe, clock, events);
        var stop = new SupervisionFailureHostStop(supervisor, lifetime, options, exitCode, events, NullLogger<SupervisionFailureHostStop>.Instance);
        await stop.StartAsync(CancellationToken.None);

        await supervisor.StartAsync(CancellationToken.None);
        await Eventually.IsTrueAsync(() => lifetime.StopRequested, "the breaker to trip and the host to be asked to stop");

        Assert.Equal(SupervisionState.Failed, supervisor.State);
        Assert.Equal(BackendState.Faulted, supervisor.Status.State);
        Assert.Contains("circuit breaker tripped", supervisor.Status.LastError, StringComparison.Ordinal);
        Assert.Equal(2, launcher.StartCount);

        // Visible to the process, to SCM, and in the Event Log, in that order
        // of who is likely to look.
        Assert.Equal(70, exitCode.ExitCode);
        Assert.True(stop.StopRequested);
        Assert.True(events.Contains(ChaosEventId.CircuitBreakerTripped));
        Assert.True(events.Contains(ChaosEventId.HostStoppingAfterSupervisionFailure));

        // Stopping the host afterwards leaves the verdict alone: Failed does
        // not soften into Stopped - not on the host's stop, and not on the
        // second stop that disposal sends after it.
        await supervisor.StopAsync(CancellationToken.None);
        Assert.Equal(SupervisionState.Failed, supervisor.State);
        await supervisor.StopAsync(CancellationToken.None);
        await supervisor.DisposeAsync();
        Assert.Equal(SupervisionState.Failed, supervisor.State);
        Assert.Equal(BackendState.Faulted, supervisor.Status.State);
    }

    [Fact]
    public async Task With_the_stop_turned_off_the_host_stays_up_and_the_failure_is_reported()
    {
        var options = Options();
        options.MaxStartFailures = 2;
        options.StopHostOnCircuitBreakerTrip = false;

        var launcher = new FakeProcessLauncher { Configure = process => process.Exit(1) };
        var clock = new FakeClock { AutoAdvance = true };
        var events = new RecordingEventSink();
        var lifetime = new FakeHostApplicationLifetime();
        var exitCode = new RecordingExitCodeReporter();

        await using var supervisor = Build(options, launcher, new FakeHealthProbe(), clock, events);
        var stop = new SupervisionFailureHostStop(supervisor, lifetime, options, exitCode, events, NullLogger<SupervisionFailureHostStop>.Instance);
        await stop.StartAsync(CancellationToken.None);

        await supervisor.StartAsync(CancellationToken.None);
        await Eventually.IsTrueAsync(() => supervisor.State == SupervisionState.Failed, "the breaker to trip");

        Assert.False(lifetime.StopRequested);
        Assert.Null(exitCode.ExitCode);
        Assert.True(events.Contains(ChaosEventId.CircuitBreakerTripped));
        Assert.False(events.Contains(ChaosEventId.HostStoppingAfterSupervisionFailure));
    }

    [Fact]
    public void A_zero_exit_code_is_refused_when_the_stop_is_on()
    {
        var options = Options();
        options.CircuitBreakerExitCode = 0;

        Assert.Contains(options.Validate(), error => error.Contains("CircuitBreakerExitCode", StringComparison.Ordinal));

        options.StopHostOnCircuitBreakerTrip = false;
        Assert.Empty(options.Validate());
    }

    /// <summary>
    /// A supervisor over the fakes. Without <paramref name="stopRequester"/>
    /// the backend has no stop endpoint (a 404), so a stop takes the signal
    /// path exactly as it did before the endpoint existed.
    /// </summary>
    internal static BackendSupervisor Build(
        BackendSupervisorOptions options,
        FakeProcessLauncher launcher,
        FakeHealthProbe probe,
        FakeClock clock,
        IChaosEventSink events,
        IBackendStopRequester? stopRequester = null) =>
        new(
            options,
            new FakeRuntimeResolver(),
            launcher,
            probe,
            stopRequester ?? new FakeStopRequester(),
            clock,
            events,
            NullLogger<BackendSupervisor>.Instance,
            NoJitterSource.Instance);
}
