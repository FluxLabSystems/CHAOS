using System.Collections.Concurrent;
using Chaos.Host.Supervisor.Events;
using Chaos.Host.Supervisor.Health;
using Chaos.Host.Supervisor.Policy;
using Chaos.Host.Supervisor.Processes;
using Chaos.Host.Supervisor.Runtime;
using Chaos.Host.Supervisor.Tests.Fakes;
using Chaos.Host.Supervisor.Time;
using Microsoft.Extensions.Logging.Abstractions;
using Xunit;

namespace Chaos.Host.Supervisor.Tests;

/// <summary>
/// The real launcher against a real child: the one place the SIGTERM path in
/// <c>GracefulSignals</c> is exercised rather than faked.
/// </summary>
/// <remarks>
/// <para>
/// The child is <c>python3 -c</c> running a script that installs a SIGTERM
/// handler, says so on stderr, and exits with a distinctive code from the
/// handler. The exit code is the evidence: a child that was killed instead of
/// asked dies with 137 (128 + SIGKILL), one whose handler never ran dies with
/// 143 (128 + SIGTERM), and only a handler that executed can produce 42.
/// </para>
/// <para>
/// Skipped, with the reason, on Windows - where the supervisor's stop paths are
/// the loopback endpoint and the Job Object, not a signal - and where no
/// <c>python3</c> is on PATH.
/// </para>
/// </remarks>
public sealed class SystemProcessLauncherTests
{
    private const int HandlerExitCode = 42;

    // stderr rather than stdout for the two marker lines, because the
    // supervisor keeps a tail of the child's stderr in its snapshot, which is
    // how the end-to-end test waits for the handler to be installed before it
    // asks for the stop.
    private const string Script =
        "import signal, sys, time\n" +
        "def on_term(signum, frame):\n" +
        "    print('sigterm handled', file=sys.stderr, flush=True)\n" +
        "    sys.exit(42)\n" +
        "signal.signal(signal.SIGTERM, on_term)\n" +
        "print('ready', file=sys.stderr, flush=True)\n" +
        "while True:\n" +
        "    time.sleep(0.05)\n";

    [RequiresUnixPython3Fact]
    public async Task Sigterm_reaches_a_real_child_and_its_handler_decides_the_exit_code()
    {
        var python = RequiresUnixPython3FactAttribute.FindPython3()!;
        var launcher = new SystemProcessLauncher();
        var lines = new ConcurrentQueue<ProcessOutputLine>();
        var ready = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);

        var spec = new ProcessStartSpec
        {
            FileName = python,
            Arguments = ["-c", Script],
            WorkingDirectory = Path.GetTempPath(),
            OnOutput = line =>
            {
                lines.Enqueue(line);
                if (line.Text == "ready")
                {
                    ready.TrySetResult();
                }
            },
        };

        using var process = launcher.Start(spec);
        await ready.Task.WaitAsync(TimeSpan.FromSeconds(30));
        Assert.False(process.HasExited);

        Assert.True(process.TryRequestGracefulShutdown(out var detail), detail);
        Assert.Contains("SIGTERM", detail, StringComparison.Ordinal);

        var code = await process.WaitForExitAsync(CancellationToken.None).WaitAsync(TimeSpan.FromSeconds(30));
        Assert.Equal(HandlerExitCode, code);
        Assert.Equal(HandlerExitCode, process.ExitCode);
        Assert.Contains(lines, line => line.Stream == ProcessOutputStream.StandardError && line.Text == "sigterm handled");
    }

    [RequiresUnixPython3Fact]
    public async Task The_supervisor_stops_a_real_child_gracefully_when_the_endpoint_is_absent()
    {
        var python = RequiresUnixPython3FactAttribute.FindPython3()!;

        // Everything between the three seams is the shipping code: real
        // launcher, real clock, real stop sequence. The health probe is faked
        // because the script serves nothing, and the stop endpoint is faked as
        // absent so the stop has to take the signal path.
        var resolver = new FakeRuntimeResolver();
        resolver.Use(new BackendRuntimeDescriptor
        {
            Layout = BackendRuntimeLayout.Development,
            Executable = python,
            // `-c <script>` consumes the script; the serve arguments the
            // supervisor appends land in sys.argv and are ignored.
            BaseArguments = ["-c", Script],
            WorkingDirectory = Path.GetTempPath(),
            Environment = new Dictionary<string, string?>(StringComparer.Ordinal),
            Description = "real python3 running a SIGTERM-trapping script (test)",
        });

        var probe = new FakeHealthProbe();
        probe.SetHealthy();
        var events = new RecordingEventSink();

        var options = new BackendSupervisorOptions
        {
            Port = 8089,
            HealthPollInterval = TimeSpan.FromMilliseconds(100),
            GracefulShutdownTimeout = TimeSpan.FromSeconds(20),
            WaitForReadyOnStart = false,
            BackoffJitterFraction = 0.0,
        };

        await using var supervisor = new BackendSupervisor(
            options,
            resolver,
            new SystemProcessLauncher(),
            probe,
            new FakeStopRequester(),
            SystemClock.Instance,
            events,
            NullLogger<BackendSupervisor>.Instance,
            NoJitterSource.Instance);

        await supervisor.StartAsync(CancellationToken.None);
        await Eventually.IsTrueAsync(
            () => supervisor.State == SupervisionState.Running
                  && supervisor.Snapshot.StandardErrorTail.Contains("ready"),
            "the real child to be running with its SIGTERM handler installed",
            TimeSpan.FromSeconds(30));

        await supervisor.StopAsync(CancellationToken.None);

        var snapshot = supervisor.Snapshot;
        Assert.Equal(SupervisionState.Stopped, snapshot.State);
        Assert.Equal(HandlerExitCode, snapshot.LastExitCode);
        Assert.Contains("stopped on request via a process signal", snapshot.LastExitDetail, StringComparison.Ordinal);
        Assert.Contains("sigterm handled", snapshot.StandardErrorTail);
        Assert.False(events.Contains(ChaosEventId.ForcedTermination), "the child must not have been killed");
        Assert.True(events.Contains(ChaosEventId.SupervisorStopped));
    }
}

/// <summary>
/// A fact that runs only where a real SIGTERM test can: a Unix-like OS with
/// <c>python3</c> on PATH. Elsewhere it is skipped with the reason, which xunit
/// reports; it never passes vacuously.
/// </summary>
[AttributeUsage(AttributeTargets.Method, AllowMultiple = false)]
public sealed class RequiresUnixPython3FactAttribute : FactAttribute
{
    public RequiresUnixPython3FactAttribute()
    {
        if (OperatingSystem.IsWindows())
        {
            Skip = "SIGTERM is the Unix stop path; on Windows the supervisor uses the loopback stop endpoint and the Job Object.";
        }
        else if (FindPython3() is null)
        {
            Skip = "python3 is not on PATH; the real-process SIGTERM test needs an interpreter to trap the signal.";
        }
    }

    /// <summary>The first <c>python3</c> on PATH, or null.</summary>
    public static string? FindPython3()
    {
        var path = Environment.GetEnvironmentVariable("PATH");
        if (string.IsNullOrEmpty(path))
        {
            return null;
        }

        foreach (var directory in path.Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries))
        {
            var candidate = Path.Combine(directory, "python3");
            if (File.Exists(candidate))
            {
                return candidate;
            }
        }

        return null;
    }
}
