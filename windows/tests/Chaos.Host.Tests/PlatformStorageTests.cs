using Chaos.Host.Configuration;

namespace Chaos.Host.Tests;

/// <summary>
/// One answer for the database and the design package, with its provenance.
/// </summary>
public sealed class PlatformStorageTests
{
    private static readonly InstalledLayout Installed = new()
    {
        Source = "configuration",
        Searched = [],
        ConfigDirectory = "/data/Project CHAOS/config",
        OperatorDataDirectory = "/data/Project CHAOS",
        LogDirectory = "/data/Project CHAOS/logs",
        InstallDirectory = "/opt/chaos",
    };

    private static readonly IReadOnlyDictionary<string, string> NoEnvironment =
        new Dictionary<string, string>(StringComparer.Ordinal);

    [Fact]
    public void The_gateway_setting_beats_everything()
    {
        var storage = PlatformStorage.Resolve(
            "sqlite:///gateway.db",
            "/gateway/data",
            "sqlite:///backend.db",
            "/backend/data",
            new Dictionary<string, string>(StringComparer.Ordinal) { ["CHAOS_DATABASE_URL"] = "sqlite:///env.db" },
            Installed);

        Assert.Equal("sqlite:///gateway.db", storage.DatabaseUrl);
        Assert.Equal("Chaos:DatabaseUrl", storage.DatabaseUrlSource);
        Assert.Equal("/gateway/data", storage.DataDirectory);
        Assert.Equal("Chaos:DataDirectory", storage.DataDirectorySource);
    }

    [Fact]
    public void The_supervisor_section_is_next_which_is_what_the_desktop_shell_sets()
    {
        var storage = PlatformStorage.Resolve(null, " ", "sqlite:///backend.db", "/backend/data", NoEnvironment, Installed);

        Assert.Equal("sqlite:///backend.db", storage.DatabaseUrl);
        Assert.Equal("Chaos:Backend:DatabaseUrl", storage.DatabaseUrlSource);
        Assert.Equal("/backend/data", storage.DataDirectory);
        Assert.Equal("Chaos:Backend:DataDirectory", storage.DataDirectorySource);
    }

    [Fact]
    public void Then_the_installers_env_file()
    {
        var environment = new Dictionary<string, string>(StringComparer.Ordinal)
        {
            ["CHAOS_DATABASE_URL"] = "postgresql+psycopg://chaos@db/homestead",
            ["CHAOS_DATA_DIR"] = "/srv/design",
        };

        var storage = PlatformStorage.Resolve(null, null, null, null, environment, Installed);

        Assert.Equal("postgresql+psycopg://chaos@db/homestead", storage.DatabaseUrl);
        Assert.Contains("homestead.env", storage.DatabaseUrlSource, StringComparison.Ordinal);
        Assert.Equal("/srv/design", storage.DataDirectory);
        Assert.Contains("CHAOS_DATA_DIR", storage.DataDirectorySource, StringComparison.Ordinal);
    }

    [Fact]
    public void Then_the_installed_layouts_defaults()
    {
        var storage = PlatformStorage.Resolve(null, null, null, null, NoEnvironment, Installed);

        Assert.Equal(Installed.DefaultDatabaseUrl, storage.DatabaseUrl);
        Assert.StartsWith("installed layout default", storage.DatabaseUrlSource, StringComparison.Ordinal);
        Assert.Equal(Installed.DefaultDataDirectory, storage.DataDirectory);
        Assert.StartsWith("installed layout default", storage.DataDirectorySource, StringComparison.Ordinal);
    }

    [Theory]
    [InlineData("postgresql+psycopg://chaos:secret@db:5432/homestead", "postgresql+psycopg://chaos:***@db:5432/homestead")]
    [InlineData("postgresql://chaos@db/homestead", "postgresql://chaos@db/homestead")]
    [InlineData("sqlite:///C:/ProgramData/Project CHAOS/db/homestead.db", "sqlite:///C:/ProgramData/Project CHAOS/db/homestead.db")]
    [InlineData("mysql://u:p@ss:w@rd@host/db", "mysql://u:***@host/db")]
    [InlineData("not a url", "not a url")]
    [InlineData(null, null)]
    public void A_password_never_reaches_a_status_page(string? url, string? expected)
    {
        Assert.Equal(expected, PlatformStorage.Redact(url));
    }

    [Fact]
    public void A_checkout_names_nothing_and_says_so()
    {
        var storage = PlatformStorage.Resolve(null, null, null, null, NoEnvironment, InstalledLayout.NotInstalled);

        Assert.Null(storage.DatabaseUrl);
        Assert.StartsWith("platform default", storage.DatabaseUrlSource, StringComparison.Ordinal);
        Assert.Null(storage.DataDirectory);
        Assert.StartsWith("platform default", storage.DataDirectorySource, StringComparison.Ordinal);
    }
}
