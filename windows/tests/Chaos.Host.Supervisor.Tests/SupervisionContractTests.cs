using Chaos.Host.Abstractions;
using Chaos.Host.Supervisor.Runtime;
using Xunit;

namespace Chaos.Host.Supervisor.Tests;

/// <summary>
/// The mapping from the supervisor's five states to the gateway's contract.
/// </summary>
/// <remarks>
/// What <c>/host/info</c> shows for each supervision state is what the shell
/// renders, so this is pinned per state rather than left to whatever the
/// switch happens to do. The property that matters most: nothing short of
/// <see cref="SupervisionState.Running"/> may ever read as
/// <see cref="BackendState.Running"/>.
/// </remarks>
public sealed class SupervisionContractTests
{
    private static BackendSnapshot Snapshot(SupervisionState state, string detail = "detail") => new()
    {
        State = state,
        StatusDetail = detail,
        RestartCount = 2,
        ProcessId = 4242,
        LastExitCode = 1,
        RunningSince = new DateTimeOffset(2026, 3, 1, 12, 0, 0, TimeSpan.Zero),
        RuntimeLayout = BackendRuntimeLayout.Embedded,
    };

    [Fact]
    public void Running_is_the_only_state_that_maps_to_running()
    {
        foreach (var state in Enum.GetValues<SupervisionState>())
        {
            var status = SupervisionContract.ToStatus(Snapshot(state));
            Assert.Equal(state == SupervisionState.Running, status.State == BackendState.Running);
        }
    }

    [Fact]
    public void Running_carries_the_pid_start_time_and_restart_count()
    {
        var status = SupervisionContract.ToStatus(Snapshot(SupervisionState.Running, "pid 4242 answered /health"));

        Assert.Equal(BackendState.Running, status.State);
        Assert.Equal(4242, status.ProcessId);
        Assert.Equal(new DateTimeOffset(2026, 3, 1, 12, 0, 0, TimeSpan.Zero), status.StartedUtc);
        Assert.Equal(2, status.RestartCount);
        Assert.Equal("pid 4242 answered /health", status.Detail);
        Assert.Null(status.LastError);
    }

    [Fact]
    public void A_scheduled_restart_reads_as_starting_with_the_reason_retained()
    {
        var status = SupervisionContract.ToStatus(
            Snapshot(SupervisionState.Restarting, "the backend exited with code 1; restart 1 in 2s"));

        Assert.Equal(BackendState.Starting, status.State);
        Assert.Equal("the backend exited with code 1; restart 1 in 2s", status.LastError);
        Assert.Equal(1, status.LastExitCode);
    }

    [Fact]
    public void A_first_start_reads_as_starting_with_no_error()
    {
        var status = SupervisionContract.ToStatus(Snapshot(SupervisionState.Starting));

        Assert.Equal(BackendState.Starting, status.State);
        Assert.Null(status.LastError);
    }

    [Fact]
    public void A_tripped_breaker_is_faulted_with_its_reason()
    {
        var snapshot = Snapshot(SupervisionState.Failed, "gave up") with
        {
            FailureReason = "circuit breaker tripped: 5 failures within 10m",
        };

        var status = SupervisionContract.ToStatus(snapshot);

        Assert.Equal(BackendState.Faulted, status.State);
        Assert.Equal("circuit breaker tripped: 5 failures within 10m", status.LastError);
        Assert.Equal(2, status.RestartCount);
    }

    [Fact]
    public void Stopped_is_stopped_with_the_last_exit_code()
    {
        var status = SupervisionContract.ToStatus(Snapshot(SupervisionState.Stopped));

        Assert.Equal(BackendState.Stopped, status.State);
        Assert.Equal(1, status.LastExitCode);
    }

    [Fact]
    public void A_stop_in_progress_reads_as_stopping_unless_the_state_is_already_terminal()
    {
        Assert.Equal(BackendState.Stopping, SupervisionContract.ToStatus(Snapshot(SupervisionState.Running), stopping: true).State);
        Assert.Equal(BackendState.Stopping, SupervisionContract.ToStatus(Snapshot(SupervisionState.Restarting), stopping: true).State);

        // Failed stays Faulted and Stopped stays Stopped: a stop request does
        // not soften a terminal failure into "winding down".
        Assert.Equal(BackendState.Faulted, SupervisionContract.ToStatus(Snapshot(SupervisionState.Failed), stopping: true).State);
        Assert.Equal(BackendState.Stopped, SupervisionContract.ToStatus(Snapshot(SupervisionState.Stopped), stopping: true).State);
    }
}
