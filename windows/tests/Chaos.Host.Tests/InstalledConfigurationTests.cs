using Chaos.Host.Configuration;
using Chaos.Host.Supervisor;
using Chaos.Host.Tests.Support;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Options;

namespace Chaos.Host.Tests;

/// <summary>
/// The gateway reads what the installer writes: <c>chaos.host.json</c> as a
/// configuration source, <c>homestead.env</c> into the backend's environment,
/// and the ProgramData database as the default.
/// </summary>
/// <remarks>
/// Pointed at a temp directory through <c>Chaos:ConfigDirectory</c>, which
/// is the same path the registry takes on an installed Windows node; the
/// registry itself is not testable here.
/// </remarks>
public sealed class InstalledConfigurationTests
{
    [Fact]
    public async Task The_installed_host_configuration_file_is_read_and_env_vars_still_win()
    {
        using var temp = TempInstalledLayout.Create()
            .WithHostConfiguration("""
                {
                  "Chaos": {
                    "BackendHealthLogQuietPeriod": "00:07:00",
                    "Backend": { "LogLevel": "DEBUG", "StandardErrorTailLines": 7 }
                  }
                }
                """);

        var backend = new FakeSupervisedBackend();
        using var factory = ChaosHostFactory.For()
            .WithSupervisedBackend(backend)
            .With("ConfigDirectory", temp.ConfigDirectory)
            .Build();

        var options = factory.Services.GetRequiredService<IOptions<ChaosHostOptions>>().Value;
        Assert.Equal(TimeSpan.FromMinutes(7), options.BackendHealthLogQuietPeriod);
        Assert.Equal(temp.ConfigDirectory, options.ConfigDirectory);
        Assert.Equal(Path.Combine(temp.Root, "logs"), options.LogDirectory);

        var supervisor = factory.Services.GetRequiredService<IOptions<BackendSupervisorOptions>>().Value;
        Assert.Equal("DEBUG", supervisor.LogLevel);
        Assert.Equal(7, supervisor.StandardErrorTailLines);

        using var client = factory.CreateClient();
        var installation = (await (await client.GetAsync("/host/info")).ReadJsonAsync()).GetProperty("installation");
        Assert.True(installation.GetProperty("installed").GetBoolean());
        Assert.Equal("configuration", installation.GetStringProperty("source"));
        Assert.True(installation.GetProperty("hostConfigurationFileLoaded").GetBoolean());
        Assert.Equal(temp.HostConfigurationFile, installation.GetStringProperty("hostConfigurationFile"));

        // No homestead.env was written: reported as a problem, not an error.
        Assert.Empty(installation.GetProperty("backendEnvironmentVariables").EnumerateArray());
        Assert.Contains(
            installation.GetProperty("backendEnvironmentProblems").EnumerateArray(),
            problem => problem.GetString()!.Contains("does not exist", StringComparison.Ordinal));
    }

    [Fact]
    public async Task Homestead_env_reaches_the_backend_and_setup_and_names_the_database()
    {
        using var temp = TempInstalledLayout.Create()
            .WithBackendEnvironment("""
                # operator overrides
                CHAOS_DATABASE_URL=sqlite:///D:/elsewhere/homestead.db
                CHAOS_MQTT_ENABLED=false
                CHAOS_MQTT_PASSWORD=hunter2
                """);

        var backend = new FakeSupervisedBackend();
        using var factory = ChaosHostFactory.For()
            .WithSupervisedBackend(backend)
            .With("ConfigDirectory", temp.ConfigDirectory)
            .Build();
        using var client = factory.CreateClient();

        var supervisor = factory.Services.GetRequiredService<IOptions<BackendSupervisorOptions>>().Value;
        Assert.Equal("sqlite:///D:/elsewhere/homestead.db", supervisor.DatabaseUrl);
        Assert.Equal("false", supervisor.Environment["CHAOS_MQTT_ENABLED"]);
        Assert.Equal("hunter2", supervisor.Environment["CHAOS_MQTT_PASSWORD"]);

        // The same answer reaches first-run setup's view of the world.
        var gateway = factory.Services.GetRequiredService<IOptions<ChaosHostOptions>>().Value;
        Assert.Equal("sqlite:///D:/elsewhere/homestead.db", gateway.DatabaseUrl);
        var storage = factory.Services.GetRequiredService<PlatformStorage>();
        Assert.Contains("homestead.env", storage.DatabaseUrlSource, StringComparison.Ordinal);

        var setup = await (await client.GetAsync("/host/setup")).ReadJsonAsync();
        Assert.Contains("homestead.env", setup.GetProperty("configuration").GetStringProperty("databaseUrlSource"), StringComparison.Ordinal);

        // And the launched child got the file's variables, as the installer promises.
        await TestExtensions.WaitUntilAsync(() => Task.FromResult(backend.Processes.Count > 0), "the backend to be launched");
        var environment = backend.Processes[0].Spec.Environment;
        Assert.Equal("sqlite:///D:/elsewhere/homestead.db", environment["CHAOS_DATABASE_URL"]);
        Assert.Equal("false", environment["CHAOS_MQTT_ENABLED"]);

        // Names on /host/info, never values: that file holds the MQTT password.
        var info = await (await client.GetAsync("/host/info")).ReadJsonAsync();
        var names = info.GetProperty("installation").GetProperty("backendEnvironmentVariables")
            .EnumerateArray().Select(name => name.GetString()).ToList();
        Assert.Equal(["CHAOS_DATABASE_URL", "CHAOS_MQTT_ENABLED", "CHAOS_MQTT_PASSWORD"], names);
        Assert.DoesNotContain("hunter2", info.GetRawText(), StringComparison.Ordinal);
    }

    [Fact]
    public void An_installed_node_that_names_no_database_uses_the_programdata_one()
    {
        using var temp = TempInstalledLayout.Create();

        var backend = new FakeSupervisedBackend();
        using var factory = ChaosHostFactory.For()
            .WithSupervisedBackend(backend)
            .With("ConfigDirectory", temp.ConfigDirectory)
            .Build();

        var storage = factory.Services.GetRequiredService<PlatformStorage>();
        Assert.Equal(temp.DefaultDatabaseUrl, storage.DatabaseUrl);
        Assert.StartsWith("installed layout default", storage.DatabaseUrlSource, StringComparison.Ordinal);

        // Setup and the supervised backend get the same file - the one
        // chaos.cmd uses too - rather than the platform's <checkout>/var default.
        Assert.Equal(temp.DefaultDatabaseUrl, factory.Services.GetRequiredService<IOptions<ChaosHostOptions>>().Value.DatabaseUrl);
        Assert.Equal(temp.DefaultDatabaseUrl, factory.Services.GetRequiredService<IOptions<BackendSupervisorOptions>>().Value.DatabaseUrl);
    }

    [Fact]
    public void A_checkout_with_no_layout_leaves_the_database_to_the_platform()
    {
        using var factory = ChaosHostFactory.For()
            .With("ConfigDirectory", Path.Combine(Path.GetTempPath(), "chaos-no-such-layout-" + Guid.NewGuid().ToString("N"), "config"))
            .Build();

        // An explicit directory that does not exist is still "installed" as
        // far as discovery goes - it is where the files would be - but with
        // nothing in it the only effect is the default database path.
        var storage = factory.Services.GetRequiredService<PlatformStorage>();
        Assert.NotNull(storage.DatabaseUrl);

        using var bare = ChaosHostFactory.For().Build();
        var layout = bare.Services.GetRequiredService<InstalledLayout>();
        if (!layout.Installed)
        {
            var unconfigured = bare.Services.GetRequiredService<PlatformStorage>();
            Assert.Null(unconfigured.DatabaseUrl);
            Assert.StartsWith("platform default", unconfigured.DatabaseUrlSource, StringComparison.Ordinal);
        }
    }

    [Fact]
    public void The_shells_backend_variables_reach_the_supervisor_and_beat_the_installed_file()
    {
        // CHAOS_Backend__DatabaseUrl is what the desktop shell sets on a child
        // it starts (Chaos.Shell.Core PlatformStartPlanner.DatabaseUrlVariable).
        // It binds through the gateway's CHAOS_ provider to Chaos:Backend:*,
        // and an environment variable outranks the installed file.
        using var temp = TempInstalledLayout.Create()
            .WithHostConfiguration("""
                { "Chaos": { "Backend": { "DatabaseUrl": "sqlite:///from-file.db", "LogLevel": "DEBUG" } } }
                """);

        const string databaseVariable = "CHAOS_Backend__DatabaseUrl";
        const string logLevelVariable = "CHAOS_Backend__LogLevel";
        Environment.SetEnvironmentVariable(databaseVariable, "sqlite:///D:/chaos/from-shell.db");
        Environment.SetEnvironmentVariable(logLevelVariable, "WARNING");
        try
        {
            var backend = new FakeSupervisedBackend();
            using var factory = ChaosHostFactory.For()
                .WithSupervisedBackend(backend)
                .With("ConfigDirectory", temp.ConfigDirectory)
                .Build();

            var supervisor = factory.Services.GetRequiredService<IOptions<BackendSupervisorOptions>>().Value;
            Assert.Equal("sqlite:///D:/chaos/from-shell.db", supervisor.DatabaseUrl);
            Assert.Equal("WARNING", supervisor.LogLevel);

            var storage = factory.Services.GetRequiredService<PlatformStorage>();
            Assert.Equal("Chaos:Backend:DatabaseUrl", storage.DatabaseUrlSource);
            Assert.Equal("sqlite:///D:/chaos/from-shell.db", factory.Services.GetRequiredService<IOptions<ChaosHostOptions>>().Value.DatabaseUrl);
        }
        finally
        {
            Environment.SetEnvironmentVariable(databaseVariable, null);
            Environment.SetEnvironmentVariable(logLevelVariable, null);
        }
    }

    [Fact]
    public async Task The_backend_is_told_the_scheduled_backup_folder_unless_homestead_env_names_one()
    {
        using var temp = TempInstalledLayout.Create()
            .WithBackendEnvironment("""
                CHAOS_MQTT_ENABLED=false
                """);

        var backend = new FakeSupervisedBackend();
        using var factory = ChaosHostFactory.For()
            .WithSupervisedBackend(backend)
            .With("ConfigDirectory", temp.ConfigDirectory)
            .Build();

        var gateway = factory.Services.GetRequiredService<IOptions<ChaosHostOptions>>().Value;
        Assert.Equal(Path.Combine(temp.Root, "backups"), gateway.Backup.Directory);

        // The folder the nightly backup writes to is the folder the backend's
        // backup-age monitor is told to watch.
        var supervisor = factory.Services.GetRequiredService<IOptions<BackendSupervisorOptions>>().Value;
        Assert.Equal(gateway.Backup.Directory, supervisor.Environment[ChaosHostExtensions.BackendBackupDirectoryVariable]);

        await TestExtensions.WaitUntilAsync(() => Task.FromResult(backend.Processes.Count > 0), "the backend to be launched");
        Assert.Equal(gateway.Backup.Directory, backend.Processes[0].Spec.Environment[ChaosHostExtensions.BackendBackupDirectoryVariable]);
    }

    [Fact]
    public void A_backup_directory_in_homestead_env_wins_over_the_gateway_s()
    {
        using var temp = TempInstalledLayout.Create()
            .WithBackendEnvironment("""
                CHAOS_BACKUP_DIRECTORY=D:/archives/chaos
                """);

        using var factory = ChaosHostFactory.For()
            .WithSupervisedBackend(new FakeSupervisedBackend())
            .With("ConfigDirectory", temp.ConfigDirectory)
            .Build();

        var supervisor = factory.Services.GetRequiredService<IOptions<BackendSupervisorOptions>>().Value;
        Assert.Equal("D:/archives/chaos", supervisor.Environment[ChaosHostExtensions.BackendBackupDirectoryVariable]);
    }
}
