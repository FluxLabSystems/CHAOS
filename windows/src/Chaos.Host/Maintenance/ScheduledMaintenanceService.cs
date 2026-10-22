using System.Globalization;
using Chaos.Host.Configuration;
using Chaos.Host.Setup;
using Chaos.Host.Supervisor.Time;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.DependencyInjection.Extensions;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;

namespace Chaos.Host.Maintenance;

/// <summary>
/// Takes the nightly backup and runs the retention pass, on the installed
/// node, without anyone opening a terminal.
/// </summary>
/// <remarks>
/// <para>
/// Once a day at <see cref="BackupOptions.TimeOfDay"/> (local time) it runs
/// the platform's own <c>chaos backup --output &lt;directory&gt;</c>, prunes
/// the directory to the newest <see cref="BackupOptions.Keep"/> archives, and
/// then runs <c>chaos retention</c> - as a dry run unless
/// <see cref="RetentionOptions.Apply"/> is set, and never with <c>--apply</c>
/// after a backup that did not succeed. Both commands go through
/// <see cref="IPlatformCommandRunner"/>, the same seam first-run setup uses, so
/// they run on the same interpreter against the same database as the
/// supervised backend, and their transcript lands in the same setup log.
/// </para>
/// <para>
/// <b>It never blocks startup and never takes the host down.</b> It is a
/// background service that waits, runs, reports on <c>GET /host/info</c>
/// (<c>maintenance</c>), and waits again. A failed backup is an error in the
/// gateway log and a <c>lastRun</c> that says so; the next run is still
/// scheduled, because the fix is for a person to make and the schedule is not
/// what is broken.
/// </para>
/// </remarks>
internal sealed partial class ScheduledMaintenanceService : BackgroundService
{
    /// <summary>The purpose recorded against the backup command.</summary>
    internal const string BackupPurpose = "scheduled backup";

    /// <summary>The purpose recorded against the retention command.</summary>
    internal const string RetentionPurpose = "scheduled retention";

    private readonly IPlatformCommandRunner _runner;
    private readonly ChaosHostOptions _options;
    private readonly MaintenanceState _state;
    private readonly IClock _clock;
    private readonly TimeZoneInfo _zone;
    private readonly ILogger<ScheduledMaintenanceService> _logger;

    private int _runCount;
    private MaintenanceRunReport? _lastRun;

    public ScheduledMaintenanceService(
        IPlatformCommandRunner runner,
        IOptions<ChaosHostOptions> options,
        MaintenanceState state,
        IClock clock,
        TimeZoneInfo zone,
        ILogger<ScheduledMaintenanceService> logger)
    {
        ArgumentNullException.ThrowIfNull(options);

        _runner = runner ?? throw new ArgumentNullException(nameof(runner));
        _options = options.Value;
        _state = state ?? throw new ArgumentNullException(nameof(state));
        _clock = clock ?? throw new ArgumentNullException(nameof(clock));
        _zone = zone ?? throw new ArgumentNullException(nameof(zone));
        _logger = logger ?? throw new ArgumentNullException(nameof(logger));
    }

    /// <inheritdoc/>
    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        var backup = _options.Backup;

        if (!backup.Enabled)
        {
            Publish(MaintenanceStatus.Disabled, "Chaos:Backup:Enabled is false; this node takes no scheduled backup and runs no retention pass");
            LogDisabled();
            return;
        }

        if (string.IsNullOrWhiteSpace(backup.Directory))
        {
            Publish(
                MaintenanceStatus.Disabled,
                "no backup directory: this is not an installed layout and Chaos:Backup:Directory is empty, so " +
                "there is nowhere the gateway may write archives on its own");
            LogNoDirectory();
            return;
        }

        var directory = Path.GetFullPath(backup.Directory);

        while (!stoppingToken.IsCancellationRequested)
        {
            var now = _clock.UtcNow;
            var next = MaintenanceSchedule.NextRun(now, backup.TimeOfDay, _zone);
            Publish(
                MaintenanceStatus.Scheduled,
                $"next run at {Local(next)} ({_zone.Id}): chaos backup --output {directory}, then " +
                (_options.Retention.Enabled
                    ? (_options.Retention.Apply ? "chaos retention --apply" : "chaos retention --dry-run")
                    : "no retention pass (Chaos:Retention:Enabled is false)"),
                next);
            LogScheduled(Local(next), _zone.Id, directory);

            try
            {
                await _clock.Delay(next - now, stoppingToken).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                return;
            }

            if (stoppingToken.IsCancellationRequested)
            {
                return;
            }

            await RunOnceAsync(directory, stoppingToken).ConfigureAwait(false);
        }
    }

    private async Task RunOnceAsync(string directory, CancellationToken cancellationToken)
    {
        var started = _clock.UtcNow;
        var problems = new List<string>();

        Publish(MaintenanceStatus.Running, $"backing up to {directory}");
        LogRunStarting(directory);

        // The CLI writes a FILE named after the directory when the directory
        // does not exist yet, so the directory is made first. On an installed
        // node the installer already created it; this is for a configured path.
        try
        {
            Directory.CreateDirectory(directory);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            problems.Add($"could not create {directory}: {ex.GetType().Name}: {ex.Message}");
        }

        var arguments = new List<string> { "backup", "--output", directory };
        if (_options.Backup.IncludeHistory)
        {
            arguments.Add("--include-history");
        }

        var backup = await _runner.RunAsync(
            new PlatformCommandRequest
            {
                Arguments = arguments,
                Purpose = BackupPurpose,
                Timeout = _options.Backup.Timeout,
            },
            cancellationToken).ConfigureAwait(false);

        string? archive = null;
        string? digest = null;
        IReadOnlyList<string> pruned = [];
        var kept = 0;

        if (backup.Succeeded)
        {
            archive = BackupArchives.ArchivePathFrom(backup.StandardOutput);
            digest = BackupArchives.DigestFrom(backup.StandardOutput);

            var prune = BackupArchives.Prune(directory, _options.Backup.Keep);
            pruned = prune.Deleted;
            kept = prune.Kept.Count;
            problems.AddRange(prune.Problems);

            LogBackupSucceeded(archive ?? "(the CLI did not print the path)", pruned.Count, kept, _options.Backup.Keep);
        }
        else
        {
            LogBackupFailed(backup.Error ?? "no error text", backup.CommandLine);
        }

        MaintenanceCommandReport? retentionReport = null;
        string? retentionSkipped = null;
        var applied = false;

        if (!_options.Retention.Enabled)
        {
            retentionSkipped = "Chaos:Retention:Enabled is false";
        }
        else if (cancellationToken.IsCancellationRequested)
        {
            retentionSkipped = "the gateway is shutting down";
        }
        else if (_options.Retention.Apply && !backup.Succeeded)
        {
            retentionSkipped =
                "retention --apply was not run because the backup did not succeed; nothing is deleted " +
                "without a fresh archive in hand";
            LogRetentionSkippedAfterFailedBackup();
        }
        else
        {
            applied = _options.Retention.Apply;
            Publish(MaintenanceStatus.Running, applied ? "applying the retention policy" : "computing the retention dry run");

            var retention = await _runner.RunAsync(
                new PlatformCommandRequest
                {
                    Arguments = applied ? ["retention", "--apply"] : ["retention", "--dry-run"],
                    Purpose = RetentionPurpose,
                    Timeout = _options.Retention.Timeout,
                },
                cancellationToken).ConfigureAwait(false);

            retentionReport = MaintenanceCommandReport.From(retention);
            if (retention.Succeeded)
            {
                LogRetentionFinished(applied ? "applied" : "dry run", string.Join(" | ", retentionReport.OutputTail));
            }
            else
            {
                LogRetentionFailed(retention.Error ?? "no error text", retention.CommandLine);
            }
        }

        var completed = _clock.UtcNow;
        var report = new MaintenanceRunReport(
            started,
            completed,
            MaintenanceCommandReport.From(backup),
            archive,
            digest,
            pruned,
            kept,
            retentionReport,
            applied,
            retentionSkipped,
            problems,
            Summarise(backup.Succeeded, archive, pruned.Count, kept, retentionReport, applied, retentionSkipped, problems.Count));

        _runCount++;
        _lastRun = report;
        Publish(MaintenanceStatus.Running, "finishing");
    }

    private static string Summarise(
        bool backupSucceeded,
        string? archive,
        int prunedCount,
        int kept,
        MaintenanceCommandReport? retention,
        bool applied,
        string? retentionSkipped,
        int problems)
    {
        var parts = new List<string>
        {
            backupSucceeded
                ? $"Backup written to {archive ?? "the backup directory"}; {prunedCount.ToString(CultureInfo.InvariantCulture)} older archive(s) pruned, {kept.ToString(CultureInfo.InvariantCulture)} kept."
                : "The backup FAILED; see the backup command's error.",
        };

        if (retention is not null)
        {
            parts.Add(retention.Outcome == PlatformCommandOutcome.Succeeded
                ? (applied ? "Retention applied." : "Retention dry run reported; nothing was deleted.")
                : "The retention pass FAILED; see its error.");
        }
        else if (retentionSkipped is not null)
        {
            parts.Add($"Retention skipped: {retentionSkipped}.");
        }

        if (problems > 0)
        {
            parts.Add($"{problems.ToString(CultureInfo.InvariantCulture)} problem(s) recorded.");
        }

        return string.Join(' ', parts);
    }

    private void Publish(MaintenanceStatus status, string detail, DateTimeOffset? nextRunUtc = null) =>
        _state.Publish(new MaintenanceSnapshot
        {
            Status = status,
            Detail = detail,
            NextRunUtc = nextRunUtc,
            RunCount = _runCount,
            LastRun = _lastRun,
        });

    private string Local(DateTimeOffset instant) =>
        TimeZoneInfo.ConvertTime(instant, _zone).ToString("yyyy-MM-dd HH:mm zzz", CultureInfo.InvariantCulture);

    [LoggerMessage(EventId = 3401, Level = LogLevel.Information,
        Message = "Scheduled backup and retention are turned off on this node (Chaos:Backup:Enabled=false).")]
    private partial void LogDisabled();

    [LoggerMessage(EventId = 3402, Level = LogLevel.Warning,
        Message = "No scheduled backup: this is not an installed layout and Chaos:Backup:Directory is empty. "
                + "Set Chaos:Backup:Directory to take nightly backups on this node.")]
    private partial void LogNoDirectory();

    [LoggerMessage(EventId = 3403, Level = LogLevel.Information,
        Message = "Next scheduled backup at {NextRunLocal} ({TimeZone}) into {Directory}.")]
    private partial void LogScheduled(string nextRunLocal, string timeZone, string directory);

    [LoggerMessage(EventId = 3404, Level = LogLevel.Information,
        Message = "Scheduled maintenance starting: backup into {Directory}, then retention.")]
    private partial void LogRunStarting(string directory);

    [LoggerMessage(EventId = 3405, Level = LogLevel.Information,
        Message = "Scheduled backup written: {Archive}. Pruned {Pruned} older archive(s); {Kept} kept (limit {Keep}).")]
    private partial void LogBackupSucceeded(string archive, int pruned, int kept, int keep);

    [LoggerMessage(EventId = 3406, Level = LogLevel.Error,
        Message = "The scheduled backup FAILED: {Error} ({CommandLine}). GET /host/info (maintenance.lastRun) carries "
                + "the details; the next run is still scheduled.")]
    private partial void LogBackupFailed(string error, string commandLine);

    [LoggerMessage(EventId = 3407, Level = LogLevel.Warning,
        Message = "Retention --apply was NOT run because the backup did not succeed. Nothing is deleted without a "
                + "fresh archive in hand.")]
    private partial void LogRetentionSkippedAfterFailedBackup();

    [LoggerMessage(EventId = 3408, Level = LogLevel.Information,
        Message = "Scheduled retention finished ({Mode}): {Counts}")]
    private partial void LogRetentionFinished(string mode, string counts);

    [LoggerMessage(EventId = 3409, Level = LogLevel.Error,
        Message = "The scheduled retention pass FAILED: {Error} ({CommandLine}).")]
    private partial void LogRetentionFailed(string error, string commandLine);
}

/// <summary>Registers the scheduled maintenance.</summary>
internal static class MaintenanceServices
{
    /// <summary>
    /// Adds the state, the clock and time zone it reads, and the hosted
    /// service. The clock is the supervisor's <see cref="IClock"/>, registered
    /// with <c>TryAdd</c> so a node with supervision on shares the one the
    /// supervisor already has, and tests can replace it.
    /// </summary>
    /// <param name="services">The service collection.</param>
    /// <returns>The collection, for chaining.</returns>
    public static IServiceCollection AddScheduledMaintenance(this IServiceCollection services)
    {
        ArgumentNullException.ThrowIfNull(services);

        services.TryAddSingleton<IClock>(SystemClock.Instance);
        services.TryAddSingleton(TimeZoneInfo.Local);
        services.TryAddSingleton<MaintenanceState>();
        services.AddHostedService<ScheduledMaintenanceService>();

        return services;
    }
}
