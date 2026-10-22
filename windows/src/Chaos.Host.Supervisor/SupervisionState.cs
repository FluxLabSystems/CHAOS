using Chaos.Host.Abstractions;

namespace Chaos.Host.Supervisor;

/// <summary>
/// The supervisor's own view of the backend lifecycle.
/// </summary>
/// <remarks>
/// <para>
/// Finer-grained than the gateway's <see cref="BackendState"/>: it keeps
/// "a restart is scheduled" apart from "starting for the first time", and a
/// tripped circuit breaker apart from a deliberate stop. The gateway sees the
/// contract shape, derived by <see cref="SupervisionContract.ToStatus"/>; the
/// shell and the diagnostics snapshot see this.
/// </para>
/// <para>
/// There is no "probably fine". <see cref="Running"/> is only ever reported
/// when <c>GET /health</c> answered, and <see cref="Failed"/> is terminal: the
/// supervisor has stopped retrying and the host is on its way down so the
/// service manager can take over.
/// </para>
/// </remarks>
public enum SupervisionState
{
    /// <summary>Not started, or stopped on request. Nothing is running.</summary>
    Stopped = 0,

    /// <summary>
    /// A process exists (or is about to) but <c>/health</c> has not answered.
    /// Also used while a running backend's health checks are failing but the
    /// supervisor has not yet given up on it — never claim health we do not have.
    /// </summary>
    Starting = 1,

    /// <summary>The backend answered <c>/health</c>. The only state that means "serving".</summary>
    Running = 2,

    /// <summary>The backend died or went unhealthy; a restart is scheduled or under way.</summary>
    Restarting = 3,

    /// <summary>
    /// Terminal. The circuit breaker tripped, the runtime could not be resolved,
    /// or shutdown found an unrecoverable condition. The supervisor is not
    /// retrying and will not recover without intervention.
    /// </summary>
    Failed = 4,
}

/// <summary>
/// Translates the supervisor's snapshot into the gateway's
/// <see cref="BackendStatus"/> contract.
/// </summary>
/// <remarks>
/// Kept as a pure function so the mapping is testable without a supervisor:
/// what the gateway reports on <c>/host/info</c> and <c>/health</c> for each
/// supervision state is a contract with the shell, and a wrong mapping would
/// read as "Running" over a dead backend.
/// </remarks>
public static class SupervisionContract
{
    /// <summary>
    /// Derives the contract status. <paramref name="stopping"/> is true while
    /// a stop has been requested and the supervision loop has not finished —
    /// a condition the snapshot itself does not carry.
    /// </summary>
    public static BackendStatus ToStatus(BackendSnapshot snapshot, bool stopping = false)
    {
        ArgumentNullException.ThrowIfNull(snapshot);

        if (stopping && snapshot.State is not (SupervisionState.Failed or SupervisionState.Stopped))
        {
            return new BackendStatus
            {
                State = BackendState.Stopping,
                Detail = "stop requested; waiting for the backend to exit",
                ProcessId = snapshot.ProcessId,
                StartedUtc = snapshot.RunningSince,
                LastExitCode = snapshot.LastExitCode,
                RestartCount = snapshot.RestartCount,
            };
        }

        return snapshot.State switch
        {
            SupervisionState.Running => new BackendStatus
            {
                State = BackendState.Running,
                Detail = snapshot.StatusDetail,
                ProcessId = snapshot.ProcessId,
                StartedUtc = snapshot.RunningSince,
                LastExitCode = snapshot.LastExitCode,
                RestartCount = snapshot.RestartCount,
            },

            // A scheduled restart is "starting" as far as the gateway is
            // concerned - nothing is serving and something is being started -
            // and the reason the last run ended is exactly what LastError is
            // for: retained so an operator can see what happened.
            SupervisionState.Starting or SupervisionState.Restarting => new BackendStatus
            {
                State = BackendState.Starting,
                Detail = snapshot.StatusDetail,
                LastError = snapshot.State == SupervisionState.Restarting ? snapshot.StatusDetail : null,
                ProcessId = snapshot.ProcessId,
                LastExitCode = snapshot.LastExitCode,
                RestartCount = snapshot.RestartCount,
            },

            SupervisionState.Failed => new BackendStatus
            {
                State = BackendState.Faulted,
                Detail = snapshot.StatusDetail,
                LastError = snapshot.FailureReason ?? snapshot.StatusDetail,
                LastExitCode = snapshot.LastExitCode,
                RestartCount = snapshot.RestartCount,
            },

            _ => new BackendStatus
            {
                State = BackendState.Stopped,
                Detail = snapshot.StatusDetail,
                LastExitCode = snapshot.LastExitCode,
                RestartCount = snapshot.RestartCount,
            },
        };
    }
}
