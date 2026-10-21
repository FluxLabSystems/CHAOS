using Chaos.Host.Supervisor;
using Chaos.Host.Supervisor.Health;
using Chaos.Host.Supervisor.Processes;
using Chaos.Host.Supervisor.Runtime;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.DependencyInjection.Extensions;

namespace Chaos.Host.Tests.Support;

/// <summary>
/// The backend as the real supervisor sees it, with every contact with the
/// machine faked: the interpreter it would resolve, the process it would
/// launch, and the <c>/health</c> it would poll.
/// </summary>
/// <remarks>
/// Everything between those three seams - the supervision loop, the restart
/// policy, the status mapping, the hosted service the gateway drives it with -
/// is the shipping code. These tests exist to prove that code is reached from
/// the gateway's composition root, which a hand-built supervisor would not.
/// </remarks>
internal sealed class FakeSupervisedBackend
{
    private readonly Lock _gate = new();
    private readonly List<FakeChildProcess> _processes = [];
    private bool _healthy = true;

    /// <summary>What the fake <c>/health</c> says. Healthy by default.</summary>
    public bool Healthy
    {
        get
        {
            lock (_gate)
            {
                return _healthy;
            }
        }

        set
        {
            lock (_gate)
            {
                _healthy = value;
            }
        }
    }

    /// <summary>Every launch the supervisor asked for, in order.</summary>
    public IReadOnlyList<FakeChildProcess> Processes
    {
        get
        {
            lock (_gate)
            {
                return [.. _processes];
            }
        }
    }

    /// <summary>Replaces the supervisor's collaborators in the host's container.</summary>
    /// <param name="services">The host's service collection.</param>
    public void Register(IServiceCollection services)
    {
        services.RemoveAll<IPythonRuntimeResolver>();
        services.RemoveAll<IProcessLauncher>();
        services.RemoveAll<IBackendHealthProbe>();

        services.AddSingleton<IPythonRuntimeResolver>(new FixedRuntimeResolver());
        services.AddSingleton<IProcessLauncher>(new Launcher(this));
        services.AddSingleton<IBackendHealthProbe>(new Probe(this));
    }

    private FakeChildProcess Launch(ProcessStartSpec spec)
    {
        lock (_gate)
        {
            var process = new FakeChildProcess(5000 + _processes.Count, spec);
            _processes.Add(process);
            return process;
        }
    }

    private sealed class Launcher(FakeSupervisedBackend owner) : IProcessLauncher
    {
        public ISupervisedProcess Start(ProcessStartSpec spec) => owner.Launch(spec);
    }

    private sealed class Probe(FakeSupervisedBackend owner) : IBackendHealthProbe
    {
        public Task<BackendHealthResult> CheckAsync(Uri endpoint, TimeSpan timeout, CancellationToken cancellationToken) =>
            Task.FromResult(owner.Healthy
                ? new BackendHealthResult(true, $"{endpoint} reported ok (fake)", "0.4.0", "primary")
                : new BackendHealthResult(false, $"{endpoint} unreachable (fake)"));
    }

    private sealed class FixedRuntimeResolver : IPythonRuntimeResolver
    {
        public BackendRuntimeResolution Resolve(BackendSupervisorOptions options) =>
            BackendRuntimeResolution.Success(
                new BackendRuntimeDescriptor
                {
                    Layout = BackendRuntimeLayout.Development,
                    Executable = "/fake/python3",
                    BaseArguments = [],
                    WorkingDirectory = "/fake/repo",
                    Environment = new Dictionary<string, string?>(StringComparer.Ordinal)
                    {
                        ["PYTHONPATH"] = "/fake/repo/src",
                    },
                    Description = "FAKE development layout for the gateway tests",
                },
                ["fake: fixed answer"]);
    }
}

/// <summary>A child process that exists only as bookkeeping.</summary>
internal sealed class FakeChildProcess : ISupervisedProcess
{
    private readonly TaskCompletionSource<int> _exit = new(TaskCreationOptions.RunContinuationsAsynchronously);

    internal FakeChildProcess(int id, ProcessStartSpec spec)
    {
        Id = id;
        Spec = spec;
    }

    public int Id { get; }

    /// <summary>Exactly what the supervisor asked to launch, environment included.</summary>
    public ProcessStartSpec Spec { get; }

    public bool HasExited { get; private set; }

    public int? ExitCode { get; private set; }

    public Task<int> WaitForExitAsync(CancellationToken cancellationToken) => _exit.Task.WaitAsync(cancellationToken);

    public bool TryRequestGracefulShutdown(out string detail)
    {
        detail = "stop requested (fake)";
        Exit(0);
        return true;
    }

    public void Kill(out string detail)
    {
        detail = "killed (fake)";
        Exit(137);
    }

    public void Dispose()
    {
        // Nothing unmanaged.
    }

    /// <summary>Makes the process exit with <paramref name="code"/>.</summary>
    public void Exit(int code)
    {
        if (HasExited)
        {
            return;
        }

        HasExited = true;
        ExitCode = code;
        _exit.TrySetResult(code);
    }
}
