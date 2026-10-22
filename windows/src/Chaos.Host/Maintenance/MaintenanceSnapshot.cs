using Chaos.Host.Configuration;
using Chaos.Host.Setup;

namespace Chaos.Host.Maintenance;

/// <summary>Where the scheduled maintenance stands.</summary>
internal enum MaintenanceStatus
{
    /// <summary>The hosted service has not run yet.</summary>
    NotStarted = 0,

    /// <summary>Switched off, or nowhere to write; <see cref="MaintenanceSnapshot.Detail"/> says which.</summary>
    Disabled = 1,

    /// <summary>Waiting for <see cref="MaintenanceSnapshot.NextRunUtc"/>.</summary>
    Scheduled = 2,

    /// <summary>A run is in progress.</summary>
    Running = 3,
}

/// <summary>One platform CLI command a maintenance run made.</summary>
/// <param name="Command">The command line, as an operator would type it.</param>
/// <param name="Outcome">How it ended.</param>
/// <param name="ExitCode">Its exit code, or null when it never ran or was killed.</param>
/// <param name="DurationSeconds">How long it took.</param>
/// <param name="Error">The failure, in the platform's words where it produced any; null on success.</param>
/// <param name="OutputTail">The last lines it printed, oldest first - the counts, for retention.</param>
internal sealed record MaintenanceCommandReport(
    string Command,
    PlatformCommandOutcome Outcome,
    int? ExitCode,
    double DurationSeconds,
    string? Error,
    IReadOnlyList<string> OutputTail)
{
    /// <summary>Lines of stdout kept on the report.</summary>
    public const int OutputTailLines = 20;

    /// <summary>Builds a report from a command result.</summary>
    public static MaintenanceCommandReport From(PlatformCommandResult result)
    {
        ArgumentNullException.ThrowIfNull(result);

        var lines = result.StandardOutput
            .Split('\n')
            .Select(line => line.TrimEnd('\r'))
            .Where(line => line.Length > 0)
            .ToList();

        return new MaintenanceCommandReport(
            result.CommandLine,
            result.Outcome,
            result.ExitCode,
            Math.Round(result.Duration.TotalSeconds, 2),
            result.Error,
            lines.Count <= OutputTailLines ? lines : lines.Skip(lines.Count - OutputTailLines).ToList());
    }
}

/// <summary>What one scheduled run did.</summary>
/// <param name="StartedUtc">When it started.</param>
/// <param name="CompletedUtc">When it finished.</param>
/// <param name="Backup">The backup command.</param>
/// <param name="Archive">The archive the CLI said it wrote, or null when it did not say.</param>
/// <param name="Sha256">The archive's digest as the CLI printed it, or null.</param>
/// <param name="PrunedArchives">Older archives removed, oldest first.</param>
/// <param name="ArchivesKept">How many archives remain in the directory.</param>
/// <param name="Retention">The retention command, or null when it did not run.</param>
/// <param name="RetentionApplied">Whether the retention command committed its deletions.</param>
/// <param name="RetentionSkipped">Why retention did not run, or null when it did.</param>
/// <param name="Problems">Anything that went wrong around the commands - pruning, the directory.</param>
/// <param name="Summary">One sentence an operator can read first.</param>
internal sealed record MaintenanceRunReport(
    DateTimeOffset StartedUtc,
    DateTimeOffset CompletedUtc,
    MaintenanceCommandReport Backup,
    string? Archive,
    string? Sha256,
    IReadOnlyList<string> PrunedArchives,
    int ArchivesKept,
    MaintenanceCommandReport? Retention,
    bool RetentionApplied,
    string? RetentionSkipped,
    IReadOnlyList<string> Problems,
    string Summary)
{
    /// <summary>Whether everything that was supposed to happen happened.</summary>
    public bool Succeeded =>
        Backup.Outcome == PlatformCommandOutcome.Succeeded
        && (Retention is null || Retention.Outcome == PlatformCommandOutcome.Succeeded)
        && Problems.Count == 0;
}

/// <summary>
/// An immutable reading of the scheduled maintenance, published by
/// <see cref="ScheduledMaintenanceService"/> and rendered on <c>GET /host/info</c>.
/// </summary>
internal sealed record MaintenanceSnapshot
{
    /// <summary>Before the hosted service has run.</summary>
    public static readonly MaintenanceSnapshot NotStarted = new()
    {
        Status = MaintenanceStatus.NotStarted,
        Detail = "the maintenance service has not started yet",
    };

    /// <summary>Where it stands.</summary>
    public required MaintenanceStatus Status { get; init; }

    /// <summary>Why, in words an operator can act on.</summary>
    public required string Detail { get; init; }

    /// <summary>The next run, or null when nothing is scheduled.</summary>
    public DateTimeOffset? NextRunUtc { get; init; }

    /// <summary>Runs completed since the gateway started.</summary>
    public int RunCount { get; init; }

    /// <summary>The most recent run, or null.</summary>
    public MaintenanceRunReport? LastRun { get; init; }
}

/// <summary>
/// A single volatile reference to the latest <see cref="MaintenanceSnapshot"/>,
/// so a request never sees a half-written reading.
/// </summary>
internal sealed class MaintenanceState
{
    private MaintenanceSnapshot _snapshot = MaintenanceSnapshot.NotStarted;

    /// <summary>The latest reading.</summary>
    public MaintenanceSnapshot Snapshot => Volatile.Read(ref _snapshot);

    /// <summary>Replaces the reading.</summary>
    public void Publish(MaintenanceSnapshot snapshot) =>
        Volatile.Write(ref _snapshot, snapshot ?? throw new ArgumentNullException(nameof(snapshot)));
}

/// <summary>Builds the <c>maintenance</c> object on <c>GET /host/info</c>.</summary>
/// <remarks>
/// The shape is a contract: fields are added, never renamed. Anything unknown
/// is a JSON <c>null</c>, never a zero that reads as "fine".
/// </remarks>
internal static class MaintenancePayload
{
    /// <summary>The payload.</summary>
    /// <param name="snapshot">The current reading.</param>
    /// <param name="options">The gateway's options, for the configuration echo.</param>
    /// <param name="zone">The zone the time of day is read in.</param>
    /// <returns>An object ready for <c>Results.Json</c>.</returns>
    public static object Create(MaintenanceSnapshot snapshot, ChaosHostOptions options, TimeZoneInfo zone)
    {
        ArgumentNullException.ThrowIfNull(snapshot);
        ArgumentNullException.ThrowIfNull(options);
        ArgumentNullException.ThrowIfNull(zone);

        var backup = options.Backup;
        var retention = options.Retention;

        return new
        {
            status = Wire(snapshot.Status),
            detail = snapshot.Detail,
            nextRunUtc = snapshot.NextRunUtc,
            nextRunLocal = snapshot.NextRunUtc is { } next ? TimeZoneInfo.ConvertTime(next, zone).ToString("yyyy-MM-dd HH:mm zzz", System.Globalization.CultureInfo.InvariantCulture) : null,
            runCount = snapshot.RunCount,
            schedule = new
            {
                enabled = backup.Enabled,
                timeOfDay = backup.TimeOfDay.ToString(@"hh\:mm", System.Globalization.CultureInfo.InvariantCulture),
                timeZone = zone.Id,
            },
            backup = new
            {
                directory = string.IsNullOrWhiteSpace(backup.Directory) ? null : backup.Directory,
                keep = backup.Keep,
                includeHistory = backup.IncludeHistory,
                timeoutSeconds = backup.Timeout.TotalSeconds,
                command = "chaos backup --output <directory>" + (backup.IncludeHistory ? " --include-history" : string.Empty),
            },
            retention = new
            {
                enabled = retention.Enabled,
                apply = retention.Apply,
                timeoutSeconds = retention.Timeout.TotalSeconds,
                command = retention.Apply ? "chaos retention --apply" : "chaos retention --dry-run",
                promise = "Retention is never applied after a failed backup, and never applied at all unless Chaos:Retention:Apply is true.",
            },
            lastRun = snapshot.LastRun is null ? null : new
            {
                succeeded = snapshot.LastRun.Succeeded,
                summary = snapshot.LastRun.Summary,
                startedUtc = snapshot.LastRun.StartedUtc,
                completedUtc = snapshot.LastRun.CompletedUtc,
                backup = Command(snapshot.LastRun.Backup),
                archive = snapshot.LastRun.Archive,
                sha256 = snapshot.LastRun.Sha256,
                prunedArchives = snapshot.LastRun.PrunedArchives,
                archivesKept = snapshot.LastRun.ArchivesKept,
                retention = snapshot.LastRun.Retention is null ? null : Command(snapshot.LastRun.Retention),
                retentionApplied = snapshot.LastRun.RetentionApplied,
                retentionSkipped = snapshot.LastRun.RetentionSkipped,
                problems = snapshot.LastRun.Problems,
            },
            environmentVariables = new[]
            {
                "CHAOS_Backup__Enabled", "CHAOS_Backup__TimeOfDay", "CHAOS_Backup__Directory", "CHAOS_Backup__Keep",
                "CHAOS_Backup__IncludeHistory", "CHAOS_Retention__Enabled", "CHAOS_Retention__Apply",
            },
        };
    }

    private static object Command(MaintenanceCommandReport report) => new
    {
        command = report.Command,
        outcome = Wire(report.Outcome),
        exitCode = report.ExitCode,
        durationSeconds = report.DurationSeconds,
        error = report.Error,
        outputTail = report.OutputTail,
    };

    private static string Wire(MaintenanceStatus status) => status switch
    {
        MaintenanceStatus.NotStarted => "not_started",
        MaintenanceStatus.Disabled => "disabled",
        MaintenanceStatus.Scheduled => "scheduled",
        MaintenanceStatus.Running => "running",
        _ => "unknown",
    };

    private static string Wire(PlatformCommandOutcome outcome) => outcome switch
    {
        PlatformCommandOutcome.Succeeded => "succeeded",
        PlatformCommandOutcome.Failed => "failed",
        PlatformCommandOutcome.TimedOut => "timed_out",
        PlatformCommandOutcome.CouldNotStart => "could_not_start",
        _ => "unknown",
    };
}
