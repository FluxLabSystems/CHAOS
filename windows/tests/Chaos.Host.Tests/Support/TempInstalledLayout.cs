namespace Chaos.Host.Tests.Support;

/// <summary>
/// A throwaway copy of what the installer lays down under
/// <c>%ProgramData%\Project CHAOS</c>: <c>config\</c>, <c>db\</c>, <c>logs\</c>,
/// and whichever of the two config files a test writes.
/// </summary>
internal sealed class TempInstalledLayout : IDisposable
{
    private TempInstalledLayout(string root)
    {
        Root = root;
        ConfigDirectory = Path.Combine(root, "config");
        Directory.CreateDirectory(ConfigDirectory);
        Directory.CreateDirectory(Path.Combine(root, "db"));
        Directory.CreateDirectory(Path.Combine(root, "logs"));
    }

    /// <summary>The ProgramData-style root.</summary>
    public string Root { get; }

    /// <summary>The <c>config</c> directory the gateway is pointed at.</summary>
    public string ConfigDirectory { get; }

    /// <summary>Full path of <c>chaos.host.json</c> in this layout.</summary>
    public string HostConfigurationFile => Path.Combine(ConfigDirectory, "chaos.host.json");

    /// <summary>Full path of <c>homestead.env</c> in this layout.</summary>
    public string BackendEnvironmentFile => Path.Combine(ConfigDirectory, "homestead.env");

    /// <summary>What the layout's default database URL must be.</summary>
    public string DefaultDatabaseUrl => "sqlite:///" + Path.Combine(Root, "db", "homestead.db").Replace('\\', '/');

    public static TempInstalledLayout Create() =>
        new(Path.Combine(Path.GetTempPath(), "chaos-host-tests-layout-" + Guid.NewGuid().ToString("N"), "Project CHAOS"));

    public TempInstalledLayout WithHostConfiguration(string json)
    {
        File.WriteAllText(HostConfigurationFile, json);
        return this;
    }

    public TempInstalledLayout WithBackendEnvironment(string text)
    {
        File.WriteAllText(BackendEnvironmentFile, text);
        return this;
    }

    public void Dispose()
    {
        try
        {
            var parent = Path.GetDirectoryName(Root);
            if (parent is not null && Directory.Exists(parent))
            {
                Directory.Delete(parent, recursive: true);
            }
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            // A temp directory that will not delete is not a test failure.
        }
    }
}
