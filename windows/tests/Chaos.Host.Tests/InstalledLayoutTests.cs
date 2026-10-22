using Chaos.Host.Configuration;
using Chaos.Host.Tests.Support;

namespace Chaos.Host.Tests;

/// <summary>
/// Finding the installed layout: the explicit setting, the registry, the
/// ProgramData default, and the honest "not installed".
/// </summary>
public sealed class InstalledLayoutTests
{
    [Fact]
    public void An_explicit_config_directory_wins_and_implies_the_rest_of_the_layout()
    {
        using var temp = TempInstalledLayout.Create();

        var layout = InstalledLayoutResolver.Resolve(temp.ConfigDirectory, null, NullInstallRegistry.Instance, programDataRoot: null);

        Assert.True(layout.Installed);
        Assert.Equal("configuration", layout.Source);
        Assert.Equal(temp.ConfigDirectory, layout.ConfigDirectory);
        Assert.Equal(temp.Root, layout.OperatorDataDirectory);
        Assert.Equal(Path.Combine(temp.Root, "logs"), layout.LogDirectory);
        Assert.Equal(temp.HostConfigurationFile, layout.HostConfigurationFile);
        Assert.Equal(temp.BackendEnvironmentFile, layout.BackendEnvironmentFile);
        Assert.Equal(temp.DefaultDatabaseUrl, layout.DefaultDatabaseUrl);

        // No install root is known without the registry, so no design-package default.
        Assert.Null(layout.InstallDirectory);
        Assert.Null(layout.DefaultDataDirectory);
        Assert.Contains(layout.Searched, line => line.StartsWith("configuration:", StringComparison.Ordinal));
    }

    [Fact]
    public void The_default_database_url_is_a_sqlalchemy_url_with_forward_slashes()
    {
        var layout = InstalledLayoutResolver.Resolve(
            Path.Combine("C:", "ProgramData", "Project CHAOS", "config"),
            null,
            NullInstallRegistry.Instance,
            programDataRoot: null);

        var url = layout.DefaultDatabaseUrl!;
        Assert.StartsWith("sqlite:///", url, StringComparison.Ordinal);
        Assert.DoesNotContain('\\', url);
        Assert.EndsWith("/db/homestead.db", url, StringComparison.Ordinal);
    }

    [Fact]
    public void The_registry_is_read_when_nothing_is_configured()
    {
        using var temp = TempInstalledLayout.Create();
        var install = Path.Combine(temp.Root, "install-root");
        var registry = new FakeInstallRegistry
        {
            ["InstallDir"] = install + Path.DirectorySeparatorChar,
            ["DataDir"] = temp.Root + Path.DirectorySeparatorChar,
            ["ConfigDir"] = temp.ConfigDirectory + Path.DirectorySeparatorChar,
            ["LogDir"] = Path.Combine(temp.Root, "logs") + Path.DirectorySeparatorChar,
        };

        var layout = InstalledLayoutResolver.Resolve(null, null, registry, programDataRoot: null);

        Assert.Equal("registry", layout.Source);
        Assert.Equal(temp.ConfigDirectory, layout.ConfigDirectory);
        Assert.Equal(temp.Root, layout.OperatorDataDirectory);
        Assert.Equal(Path.Combine(temp.Root, "logs"), layout.LogDirectory);
        Assert.Equal(install, layout.InstallDirectory);
        Assert.Equal(Path.Combine(install, "app", "data"), layout.DefaultDataDirectory);
    }

    [Fact]
    public void The_programdata_folder_is_used_only_when_its_config_directory_exists()
    {
        using var temp = TempInstalledLayout.Create();
        var programData = Path.GetDirectoryName(temp.Root)!;

        var found = InstalledLayoutResolver.Resolve(null, null, NullInstallRegistry.Instance, programData);
        Assert.Equal("programdata", found.Source);
        Assert.Equal(temp.ConfigDirectory, found.ConfigDirectory);
        Assert.Equal(Path.Combine(temp.Root, "logs"), found.LogDirectory);

        var absent = InstalledLayoutResolver.Resolve(
            null,
            null,
            NullInstallRegistry.Instance,
            Path.Combine(programData, "nothing-here"));
        Assert.False(absent.Installed);
        Assert.Equal("none", absent.Source);
        Assert.Null(absent.DefaultDatabaseUrl);
        Assert.Null(absent.LogDirectory);
        Assert.Contains(absent.Searched, line => line.StartsWith("programdata: no ", StringComparison.Ordinal));
    }

    [Fact]
    public void A_configured_log_directory_overrides_the_layouts()
    {
        using var temp = TempInstalledLayout.Create();
        var elsewhere = Path.Combine(temp.Root, "elsewhere");

        var layout = InstalledLayoutResolver.Resolve(temp.ConfigDirectory, elsewhere, NullInstallRegistry.Instance, null);

        Assert.Equal(elsewhere, layout.LogDirectory);
        Assert.Contains(layout.Searched, line => line.StartsWith("logs:", StringComparison.Ordinal));
    }

    [Fact]
    public void A_machine_with_nothing_reports_not_installed_and_what_it_tried()
    {
        var layout = InstalledLayoutResolver.Resolve(null, null, NullInstallRegistry.Instance, programDataRoot: null);

        Assert.False(layout.Installed);
        Assert.Null(layout.HostConfigurationFile);
        Assert.Null(layout.BackendEnvironmentFile);
        Assert.Equal(2, layout.Searched.Count);
    }

    private sealed class FakeInstallRegistry : Dictionary<string, string>, IInstallRegistry
    {
        public string? Read(string valueName) => TryGetValue(valueName, out var value) ? value : null;
    }
}
