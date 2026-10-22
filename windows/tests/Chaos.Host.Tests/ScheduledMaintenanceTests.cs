using Chaos.Host.Configuration;
using Chaos.Host.Maintenance;
using Chaos.Host.Setup;
using Chaos.Host.Supervisor.Tests.Fakes;
using Chaos.Host.Tests.Support;
using Microsoft.Extensions.Logging.Abstractions;
using Microsoft.Extensions.Options;

namespace Chaos.Host.Tests;

/// <summary>
/// The nightly backup and retention pass, driven against a fake platform CLI
/// and a virtual clock.
/// </summary>
/// <remarks>
/// No interpreter, no database, and no waiting until 02:00: the clock is
/// advanced to it. What the service asked the CLI for, in what order, and what
/// it declined to ask for, is the whole subject.
/// </remarks>
public sealed class ScheduledMaintenanceTests : IDisposable
{
    private static readonly DateTimeOffset TenInTheMorning = new(2026, 3, 10, 10, 0, 0, TimeSpan.Zero);
    private static readonly DateTimeOffset TwoNextMorning = new(2026, 3, 11, 2, 0, 0, TimeSpan.Zero);

    private readonly string _directory = Path.Combine(
        Path.GetTempPath(), "chaos-maintenance-tests-" + Guid.NewGuid().ToString("N"));

    public void Dispose()
    {
        try
        {
            if (Directory.Exists(_directory))
            {
                Directory.Delete(_directory, recursive: true);
            }
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            // Not a test failure.
        }
    }

    [Fact]
    public async Task The_backup_and_the_retention_dry_run_happen_at_the_configured_time_and_are_reported()
    {
        var archive = Path.Combine(_directory, "homestead-primary-20260311T020000Z.tar.gz");
        var digest = new string('a', 64);
        var runner = new FakePlatformCommandRunner((verb, _) => verb switch
        {
            "backup" => FakePlatformCommandRunner.Ok(
                $"Backup   : {archive}\nSize     : 0.10 MiB\nSHA-256  : {digest}\nTables   : 11 dumped, 1100 rows\n"),
            "retention" => FakePlatformCommandRunner.Ok(
                "Retention dry run (nothing committed).\n  telemetry_samples: 0 deleted\n  raw retention: 90 days (CHAOS_HISTORIAN_RAW_RETENTION_DAYS)\n"),
            _ => throw new InvalidOperationException($"unexpected command '{verb}'"),
        });
        var clock = new FakeClock(TenInTheMorning);
        var (service, state) = Build(runner, clock);

        await service.StartAsync(CancellationToken.None);
        await WaitForTheWaiterAsync(clock);

        Assert.Equal(MaintenanceStatus.Scheduled, state.Snapshot.Status);
        Assert.Equal(TwoNextMorning, state.Snapshot.NextRunUtc);
        Assert.Empty(runner.Invocations);

        clock.Advance(TimeSpan.FromHours(16));
        await TestExtensions.WaitUntilAsync(
            () => Task.FromResult(state.Snapshot.RunCount == 1 && state.Snapshot.Status == MaintenanceStatus.Scheduled),
            "the run to finish and the next one to be scheduled");

        // The platform's own commands, in order: the backup first, then the
        // retention DRY RUN. Never --apply unless asked.
        Assert.Equal([$"backup --output {_directory}", "retention --dry-run"], runner.Invocations);

        var run = state.Snapshot.LastRun;
        Assert.NotNull(run);
        Assert.True(run!.Succeeded);
        Assert.Equal(TwoNextMorning, run.StartedUtc);
        Assert.Equal(PlatformCommandOutcome.Succeeded, run.Backup.Outcome);
        Assert.Equal(archive, run.Archive);
        Assert.Equal(digest, run.Sha256);
        Assert.NotNull(run.Retention);
        Assert.False(run.RetentionApplied);
        Assert.Null(run.RetentionSkipped);
        Assert.Contains(run.Retention!.OutputTail, line => line.Contains("telemetry_samples", StringComparison.Ordinal));
        Assert.Contains("Retention dry run reported", run.Summary, StringComparison.Ordinal);

        // Tomorrow, same time.
        Assert.Equal(TwoNextMorning.AddDays(1), state.Snapshot.NextRunUtc);

        await service.StopAsync(CancellationToken.None);
    }

    [Fact]
    public async Task Old_archives_are_pruned_oldest_first_and_anything_else_in_the_directory_is_left_alone()
    {
        Directory.CreateDirectory(_directory);
        for (var day = 1; day <= 15; day++)
        {
            File.WriteAllText(Path.Combine(_directory, $"homestead-primary-202602{day:00}T020000Z.tar.gz"), "old");
        }

        File.WriteAllText(Path.Combine(_directory, "homestead-manual-copy.tar.gz"), "an operator's own copy");
        File.WriteAllText(Path.Combine(_directory, "notes.txt"), "do not delete");

        var runner = new FakePlatformCommandRunner((verb, _) =>
        {
            if (verb == "backup")
            {
                // The fake CLI writes its archive, as the real one would.
                var archive = Path.Combine(_directory, "homestead-primary-20260311T020000Z.tar.gz");
                File.WriteAllText(archive, "new");
                return FakePlatformCommandRunner.Ok($"Backup   : {archive}\n");
            }

            return FakePlatformCommandRunner.Ok("Retention dry run (nothing committed).\n");
        });
        var clock = new FakeClock(TenInTheMorning);
        var (service, state) = Build(runner, clock, options => options.Backup.Keep = 14);

        await service.StartAsync(CancellationToken.None);
        await WaitForTheWaiterAsync(clock);
        clock.Advance(TimeSpan.FromHours(16));
        await TestExtensions.WaitUntilAsync(() => Task.FromResult(state.Snapshot.RunCount == 1), "the run to finish");
        await service.StopAsync(CancellationToken.None);

        // 15 old + 1 new = 16; keep 14; the two oldest go.
        var run = state.Snapshot.LastRun!;
        Assert.Equal(
            [
                Path.Combine(_directory, "homestead-primary-20260201T020000Z.tar.gz"),
                Path.Combine(_directory, "homestead-primary-20260202T020000Z.tar.gz"),
            ],
            run.PrunedArchives);
        Assert.Equal(14, run.ArchivesKept);
        Assert.Empty(run.Problems);

        Assert.False(File.Exists(Path.Combine(_directory, "homestead-primary-20260201T020000Z.tar.gz")));
        Assert.True(File.Exists(Path.Combine(_directory, "homestead-primary-20260203T020000Z.tar.gz")));
        Assert.True(File.Exists(Path.Combine(_directory, "homestead-primary-20260311T020000Z.tar.gz")));
        Assert.True(File.Exists(Path.Combine(_directory, "homestead-manual-copy.tar.gz")));
        Assert.True(File.Exists(Path.Combine(_directory, "notes.txt")));
    }

    [Fact]
    public async Task Retention_is_applied_only_when_asked_and_only_after_a_backup_that_succeeded()
    {
        var runner = new FakePlatformCommandRunner((verb, call) => (verb, call) switch
        {
            ("backup", 1) => FakePlatformCommandRunner.Ok($"Backup   : {Path.Combine(_directory, "homestead-primary-20260311T020000Z.tar.gz")}\n"),
            ("retention", _) => FakePlatformCommandRunner.Ok("Retention applied.\n  telemetry_samples: 1200 deleted\n"),
            ("backup", _) => FakePlatformCommandRunner.Fails(1, "The platform CLI exited 1 (a runtime failure).\nchaos: error: disk full", "chaos: error: disk full"),
            _ => throw new InvalidOperationException($"unexpected command '{verb}'"),
        });
        var clock = new FakeClock(TenInTheMorning);
        var (service, state) = Build(runner, clock, options => options.Retention.Apply = true);

        await service.StartAsync(CancellationToken.None);
        await WaitForTheWaiterAsync(clock);

        // Night one: the backup succeeds, so --apply runs.
        clock.Advance(TimeSpan.FromHours(16));
        await TestExtensions.WaitUntilAsync(() => Task.FromResult(state.Snapshot.RunCount == 1), "the first run to finish");
        Assert.Equal([$"backup --output {_directory}", "retention --apply"], runner.Invocations);
        Assert.True(state.Snapshot.LastRun!.RetentionApplied);
        Assert.Contains("Retention applied", state.Snapshot.LastRun.Summary, StringComparison.Ordinal);

        // Night two: the backup fails, so nothing is deleted - --apply is not
        // even attempted, and the report says why.
        await WaitForTheWaiterAsync(clock);
        clock.Advance(TimeSpan.FromDays(1));
        await TestExtensions.WaitUntilAsync(() => Task.FromResult(state.Snapshot.RunCount == 2), "the second run to finish");
        await service.StopAsync(CancellationToken.None);

        Assert.Equal(3, runner.Invocations.Count);
        Assert.Equal($"backup --output {_directory}", runner.Invocations[2]);

        var run = state.Snapshot.LastRun!;
        Assert.False(run.Succeeded);
        Assert.Equal(PlatformCommandOutcome.Failed, run.Backup.Outcome);
        Assert.Contains("disk full", run.Backup.Error, StringComparison.Ordinal);
        Assert.Null(run.Retention);
        Assert.False(run.RetentionApplied);
        Assert.Contains("did not succeed", run.RetentionSkipped, StringComparison.Ordinal);
        Assert.Contains("FAILED", run.Summary, StringComparison.Ordinal);

        // And the schedule survives the failure.
        Assert.Equal(MaintenanceStatus.Scheduled, state.Snapshot.Status);
        Assert.Equal(TwoNextMorning.AddDays(2), state.Snapshot.NextRunUtc);
    }

    [Fact]
    public async Task A_failed_backup_still_lets_the_dry_run_report()
    {
        var runner = new FakePlatformCommandRunner((verb, _) => verb switch
        {
            "backup" => FakePlatformCommandRunner.CouldNotStart("No Python interpreter on PATH (python3, python) and no embedded runtime."),
            _ => FakePlatformCommandRunner.Ok("Retention dry run (nothing committed).\n"),
        });
        var clock = new FakeClock(TenInTheMorning);
        var (service, state) = Build(runner, clock);

        await service.StartAsync(CancellationToken.None);
        await WaitForTheWaiterAsync(clock);
        clock.Advance(TimeSpan.FromHours(16));
        await TestExtensions.WaitUntilAsync(() => Task.FromResult(state.Snapshot.RunCount == 1), "the run to finish");
        await service.StopAsync(CancellationToken.None);

        // A dry run deletes nothing, so it is still worth having the counts.
        Assert.Equal([$"backup --output {_directory}", "retention --dry-run"], runner.Invocations);
        var run = state.Snapshot.LastRun!;
        Assert.Equal(PlatformCommandOutcome.CouldNotStart, run.Backup.Outcome);
        Assert.Null(run.Archive);
        Assert.Empty(run.PrunedArchives);
        Assert.NotNull(run.Retention);
        Assert.False(run.Succeeded);
    }

    [Fact]
    public async Task Switched_off_means_nothing_runs_and_the_report_says_why()
    {
        var runner = new FakePlatformCommandRunner((_, _) =>
            throw new InvalidOperationException("The platform CLI must not be launched with the backup off."));
        var clock = new FakeClock(TenInTheMorning);
        var (service, state) = Build(runner, clock, options => options.Backup.Enabled = false);

        await service.StartAsync(CancellationToken.None);
        await service.ExecuteTask!;

        Assert.Equal(MaintenanceStatus.Disabled, state.Snapshot.Status);
        Assert.Contains("Chaos:Backup:Enabled", state.Snapshot.Detail, StringComparison.Ordinal);
        Assert.Null(state.Snapshot.NextRunUtc);
        Assert.Equal(0, clock.PendingWaiters);
        Assert.Empty(runner.Invocations);
    }

    [Fact]
    public async Task Nowhere_to_write_means_nothing_runs_and_the_report_says_why()
    {
        var runner = new FakePlatformCommandRunner((_, _) =>
            throw new InvalidOperationException("The platform CLI must not be launched with no backup directory."));
        var clock = new FakeClock(TenInTheMorning);
        var (service, state) = Build(runner, clock, options => options.Backup.Directory = "   ");

        await service.StartAsync(CancellationToken.None);
        await service.ExecuteTask!;

        Assert.Equal(MaintenanceStatus.Disabled, state.Snapshot.Status);
        Assert.Contains("Chaos:Backup:Directory", state.Snapshot.Detail, StringComparison.Ordinal);
        Assert.Empty(runner.Invocations);
    }

    [Fact]
    public async Task Stopping_the_host_ends_the_wait_at_once()
    {
        var runner = new FakePlatformCommandRunner((_, _) => FakePlatformCommandRunner.Ok());
        var clock = new FakeClock(TenInTheMorning);
        var (service, _) = Build(runner, clock);

        await service.StartAsync(CancellationToken.None);
        await WaitForTheWaiterAsync(clock);

        await service.StopAsync(CancellationToken.None).WaitAsync(TimeSpan.FromSeconds(10));

        Assert.True(service.ExecuteTask!.IsCompleted);
        Assert.Equal(0, clock.PendingWaiters);
        Assert.Empty(runner.Invocations);
    }

    private (ScheduledMaintenanceService Service, MaintenanceState State) Build(
        FakePlatformCommandRunner runner,
        FakeClock clock,
        Action<ChaosHostOptions>? configure = null)
    {
        var options = new ChaosHostOptions();
        options.Backup.Directory = _directory;
        configure?.Invoke(options);

        var state = new MaintenanceState();
        var service = new ScheduledMaintenanceService(
            runner,
            Options.Create(options),
            state,
            clock,
            TimeZoneInfo.Utc,
            NullLogger<ScheduledMaintenanceService>.Instance);

        return (service, state);
    }

    private static Task WaitForTheWaiterAsync(FakeClock clock) =>
        TestExtensions.WaitUntilAsync(
            () => Task.FromResult(clock.PendingWaiters == 1),
            "the service to be waiting on the clock for the next run");
}

/// <summary>When the next run is, as a pure function.</summary>
public sealed class MaintenanceScheduleTests
{
    [Fact]
    public void Today_when_the_time_is_still_ahead_and_tomorrow_once_it_has_passed()
    {
        var twoAm = TimeSpan.FromHours(2);

        Assert.Equal(
            new DateTimeOffset(2026, 3, 10, 2, 0, 0, TimeSpan.Zero),
            MaintenanceSchedule.NextRun(new DateTimeOffset(2026, 3, 10, 1, 59, 59, TimeSpan.Zero), twoAm, TimeZoneInfo.Utc));

        // Exactly on time is "already happened": strictly after, never a second run.
        Assert.Equal(
            new DateTimeOffset(2026, 3, 11, 2, 0, 0, TimeSpan.Zero),
            MaintenanceSchedule.NextRun(new DateTimeOffset(2026, 3, 10, 2, 0, 0, TimeSpan.Zero), twoAm, TimeZoneInfo.Utc));

        Assert.Equal(
            new DateTimeOffset(2026, 3, 11, 2, 0, 0, TimeSpan.Zero),
            MaintenanceSchedule.NextRun(new DateTimeOffset(2026, 3, 10, 10, 0, 0, TimeSpan.Zero), twoAm, TimeZoneInfo.Utc));
    }

    [Fact]
    public void The_time_of_day_is_read_in_the_given_zone()
    {
        // 02:00 at UTC-5 is 07:00Z. At 05:30Z it is 00:30 local: still today.
        var minusFive = TimeZoneInfo.CreateCustomTimeZone("Test-5", TimeSpan.FromHours(-5), "Test-5", "Test-5");

        Assert.Equal(
            new DateTimeOffset(2026, 3, 10, 7, 0, 0, TimeSpan.Zero),
            MaintenanceSchedule.NextRun(new DateTimeOffset(2026, 3, 10, 5, 30, 0, TimeSpan.Zero), TimeSpan.FromHours(2), minusFive));

        // At 07:00:01Z it is just after 02:00 local: tomorrow.
        Assert.Equal(
            new DateTimeOffset(2026, 3, 11, 7, 0, 0, TimeSpan.Zero),
            MaintenanceSchedule.NextRun(new DateTimeOffset(2026, 3, 10, 7, 0, 1, TimeSpan.Zero), TimeSpan.FromHours(2), minusFive));
    }

    [Fact]
    public void A_time_inside_the_spring_forward_gap_runs_after_the_jump_and_only_once()
    {
        // North American rules: second Sunday in March at 02:00 the clocks
        // jump to 03:00, so 02:30 on 2026-03-08 never happens.
        var rule = TimeZoneInfo.AdjustmentRule.CreateAdjustmentRule(
            DateTime.MinValue.Date,
            DateTime.MaxValue.Date,
            TimeSpan.FromHours(1),
            TimeZoneInfo.TransitionTime.CreateFloatingDateRule(new DateTime(1, 1, 1, 2, 0, 0), 3, 2, DayOfWeek.Sunday),
            TimeZoneInfo.TransitionTime.CreateFloatingDateRule(new DateTime(1, 1, 1, 2, 0, 0), 11, 1, DayOfWeek.Sunday));
        var eastern = TimeZoneInfo.CreateCustomTimeZone("Test-Eastern", TimeSpan.FromHours(-5), "Test-Eastern", "EST", "EDT", [rule]);

        var halfPastTwo = TimeSpan.FromHours(2.5);
        var justAfterMidnight = new DateTimeOffset(2026, 3, 8, 5, 30, 0, TimeSpan.Zero); // 00:30 EST

        // 03:30 EDT = 07:30Z, not the 07:30Z that 02:30 EST would be... which is the same instant;
        // what matters is that it is after the jump and today.
        var next = MaintenanceSchedule.NextRun(justAfterMidnight, halfPastTwo, eastern);
        Assert.Equal(new DateTimeOffset(2026, 3, 8, 7, 30, 0, TimeSpan.Zero), next);

        // And the run after that is a full day later in local terms, 02:30 EDT = 06:30Z.
        var after = MaintenanceSchedule.NextRun(next, halfPastTwo, eastern);
        Assert.Equal(new DateTimeOffset(2026, 3, 9, 6, 30, 0, TimeSpan.Zero), after);
    }

    [Fact]
    public void A_time_of_day_outside_the_clock_is_refused()
    {
        Assert.Throws<ArgumentOutOfRangeException>(() =>
            MaintenanceSchedule.NextRun(DateTimeOffset.UtcNow, TimeSpan.FromHours(24), TimeZoneInfo.Utc));
        Assert.Throws<ArgumentOutOfRangeException>(() =>
            MaintenanceSchedule.NextRun(DateTimeOffset.UtcNow, TimeSpan.FromMinutes(-1), TimeZoneInfo.Utc));
    }
}

/// <summary>Reading the platform CLI's archives back.</summary>
public sealed class BackupArchiveTests
{
    [Fact]
    public void Only_files_named_the_way_the_cli_names_them_are_archives()
    {
        Assert.True(BackupArchives.IsArchive("homestead-primary-20260311T020000Z.tar.gz"));
        Assert.True(BackupArchives.IsArchive("homestead-secondary-20260311T020000Z.tar.gz"));
        Assert.Equal("20260311T020000Z", BackupArchives.StampOf("homestead-primary-20260311T020000Z.tar.gz"));

        Assert.False(BackupArchives.IsArchive("homestead-manual-copy.tar.gz"));
        Assert.False(BackupArchives.IsArchive("homestead-primary-20260311T020000Z.tar.gz.bak"));
        Assert.False(BackupArchives.IsArchive("notes.txt"));
        Assert.Null(BackupArchives.StampOf("notes.txt"));
    }

    [Fact]
    public void The_archive_path_and_digest_are_read_from_what_the_cli_printed()
    {
        var output =
            "Backup   : C:\\ProgramData\\Project CHAOS\\backups\\homestead-primary-20260311T020000Z.tar.gz\r\n" +
            "Size     : 0.10 MiB\r\n" +
            "SHA-256  : " + new string('B', 64) + "\r\n" +
            "Tables   : 11 dumped, 1100 rows\r\n";

        Assert.Equal(
            "C:\\ProgramData\\Project CHAOS\\backups\\homestead-primary-20260311T020000Z.tar.gz",
            BackupArchives.ArchivePathFrom(output));
        Assert.Equal(new string('b', 64), BackupArchives.DigestFrom(output));

        Assert.Null(BackupArchives.ArchivePathFrom("Homestead Digital Twin 0.4.0\n"));
        Assert.Null(BackupArchives.DigestFrom(string.Empty));
    }

    [Fact]
    public void Pruning_an_empty_or_small_directory_deletes_nothing()
    {
        var directory = Path.Combine(Path.GetTempPath(), "chaos-prune-tests-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(directory);
        try
        {
            File.WriteAllText(Path.Combine(directory, "homestead-primary-20260301T020000Z.tar.gz"), "x");

            var result = BackupArchives.Prune(directory, 14);

            Assert.Empty(result.Deleted);
            Assert.Single(result.Kept);
            Assert.Empty(result.Problems);
        }
        finally
        {
            Directory.Delete(directory, recursive: true);
        }
    }

    [Fact]
    public void A_missing_directory_is_a_problem_not_an_exception()
    {
        var result = BackupArchives.Prune(Path.Combine(Path.GetTempPath(), "chaos-prune-tests-missing-" + Guid.NewGuid().ToString("N")), 14);

        Assert.Empty(result.Deleted);
        Assert.Single(result.Problems);
        Assert.Contains("could not list", result.Problems[0], StringComparison.Ordinal);
    }
}

/// <summary>The settings are refused at startup rather than at 02:00.</summary>
public sealed class MaintenanceOptionsTests
{
    [Fact]
    public void The_defaults_are_a_nightly_backup_at_two_with_fourteen_kept_and_retention_as_a_dry_run()
    {
        var options = new ChaosHostOptions();

        Assert.True(options.Backup.Enabled);
        Assert.Equal(TimeSpan.FromHours(2), options.Backup.TimeOfDay);
        Assert.Null(options.Backup.Directory);
        Assert.Equal(14, options.Backup.Keep);
        Assert.False(options.Backup.IncludeHistory);
        Assert.True(options.Retention.Enabled);
        Assert.False(options.Retention.Apply);

        var result = new ChaosHostOptionsValidator().Validate(null, options);
        Assert.True(result.Succeeded, string.Join("; ", result.Failures ?? []));
    }

    [Theory]
    [InlineData("24:00:00", "Chaos:Backup:TimeOfDay")]
    [InlineData("-00:01:00", "Chaos:Backup:TimeOfDay")]
    public void A_time_of_day_outside_the_clock_is_refused(string timeOfDay, string expected)
    {
        var options = new ChaosHostOptions();
        options.Backup.TimeOfDay = TimeSpan.Parse(timeOfDay, System.Globalization.CultureInfo.InvariantCulture);

        var result = new ChaosHostOptionsValidator().Validate(null, options);

        Assert.True(result.Failed);
        Assert.Contains(result.Failures!, failure => failure.Contains(expected, StringComparison.Ordinal));
    }

    [Fact]
    public void Keeping_no_archives_is_refused_because_it_would_delete_the_one_just_written()
    {
        var options = new ChaosHostOptions();
        options.Backup.Keep = 0;

        var result = new ChaosHostOptionsValidator().Validate(null, options);

        Assert.True(result.Failed);
        Assert.Contains(result.Failures!, failure => failure.Contains("Chaos:Backup:Keep", StringComparison.Ordinal));
    }

    [Fact]
    public void The_installed_layout_supplies_the_backup_folder_the_installer_created()
    {
        var layout = InstalledLayoutResolver.Resolve(
            Path.Combine(Path.GetTempPath(), "chaos-layout-tests", "Project CHAOS", "config"),
            null,
            NullInstallRegistry.Instance,
            null);

        Assert.Equal(
            Path.Combine(Path.GetTempPath(), "chaos-layout-tests", "Project CHAOS", "backups"),
            layout.DefaultBackupDirectory);
        Assert.Null(InstalledLayout.NotInstalled.DefaultBackupDirectory);
    }
}
