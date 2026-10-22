namespace Chaos.Host.Configuration;

/// <summary>
/// The scheduled backup, bound from <c>Chaos:Backup</c>.
/// </summary>
/// <remarks>
/// <para>
/// Once a day, at <see cref="TimeOfDay"/> local time, the gateway runs the
/// platform's own <c>chaos backup --output &lt;Directory&gt;</c> - the same
/// archive an operator gets from the command line, written by the same code -
/// and then prunes the directory down to the newest <see cref="Keep"/>
/// archives. It never reimplements the backup, and it never deletes anything
/// that the platform CLI did not name: only files matching
/// <c>homestead-&lt;role&gt;-&lt;UTC&gt;.tar.gz</c> are counted or pruned.
/// </para>
/// <para>
/// The result of the last run is on <c>GET /host/info</c> under
/// <c>maintenance</c>, and the command transcript is appended to the setup log
/// (<c>chaos-setup.log</c>), which is the gateway's record of every platform
/// CLI invocation it makes. Environment overrides use the usual form:
/// <c>CHAOS_Backup__Enabled</c>, <c>CHAOS_Backup__TimeOfDay</c>.
/// </para>
/// </remarks>
public sealed class BackupOptions
{
    /// <summary>
    /// Whether the gateway takes the nightly backup at all. On by default: an
    /// installed node that is never backed up is one disk failure away from
    /// losing its alarm and command history.
    /// </summary>
    public bool Enabled { get; set; } = true;

    /// <summary>
    /// Wall-clock time of day, in the machine's local time zone, at which the
    /// run starts. 02:00 by default - off-peak, and after the day's data is in.
    /// Written as <c>"02:00"</c> or <c>"23:30"</c>.
    /// </summary>
    public TimeSpan TimeOfDay { get; set; } = TimeSpan.FromHours(2);

    /// <summary>
    /// Where archives are written. Empty means the installed layout's
    /// <c>backups</c> folder (<c>%ProgramData%\Project CHAOS\backups</c>, which
    /// the installer creates and never removes). On a machine with no installed
    /// layout and nothing set here, the scheduled backup is off and
    /// <c>/host/info</c> says so.
    /// </summary>
    public string? Directory { get; set; }

    /// <summary>
    /// How many archives survive pruning, newest first. 14 by default: two
    /// weeks of nightly archives. Pruning only ever touches files named the way
    /// the platform CLI names them.
    /// </summary>
    public int Keep { get; set; } = 14;

    /// <summary>
    /// Pass <c>--include-history</c>, so the archive also holds the telemetry
    /// samples and dead letters. Off by default because the history is the
    /// large part and the retention policy already bounds it; the registry,
    /// configuration, alarms and commands are in every archive regardless.
    /// </summary>
    public bool IncludeHistory { get; set; }

    /// <summary>How long one backup may take before it is stopped and reported as timed out.</summary>
    public TimeSpan Timeout { get; set; } = TimeSpan.FromMinutes(30);
}

/// <summary>
/// The scheduled retention pass, bound from <c>Chaos:Retention</c>. Runs on the
/// backup's schedule, after the backup.
/// </summary>
/// <remarks>
/// <para>
/// By default it is a <b>dry run</b>: <c>chaos retention --dry-run</c> computes
/// what the policy would delete, rolls back, and the counts are reported on
/// <c>/host/info</c>. Retention is a deletion, so applying it is opt-in
/// (<see cref="Apply"/>), and even then it is applied only after the backup of
/// the same run succeeded: nothing is deleted without a fresh archive in hand.
/// </para>
/// <para>
/// The policy itself (<c>CHAOS_HISTORIAN_RAW_RETENTION_DAYS</c>) is the
/// platform's, set in <c>homestead.env</c>.
/// </para>
/// </remarks>
public sealed class RetentionOptions
{
    /// <summary>Whether the retention pass runs at all. On by default, as a dry run.</summary>
    public bool Enabled { get; set; } = true;

    /// <summary>
    /// Commit the deletions (<c>chaos retention --apply</c>) instead of
    /// reporting them. Off by default. Never applied after a failed backup.
    /// </summary>
    public bool Apply { get; set; }

    /// <summary>How long one retention pass may take before it is stopped and reported as timed out.</summary>
    public TimeSpan Timeout { get; set; } = TimeSpan.FromMinutes(30);
}
