using Chaos.Host.Abstractions;
using Chaos.Host.Supervisor.Events;
using Chaos.Host.Supervisor.Policy;
using Chaos.Host.Supervisor.Processes;
using Chaos.Host.Supervisor.Tests.Fakes;
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

    internal static BackendSupervisor Build(
        BackendSupervisorOptions options,
        FakeProcessLauncher launcher,
        FakeHealthProbe probe,
        FakeClock clock,
        IChaosEventSink events) =>
        new(
            options,
            new FakeRuntimeResolver(),
            launcher,
            probe,
            clock,
            events,
            NullLogger<BackendSupervisor>.Instance,
            NoJitterSource.Instance);
}
