using System.Runtime.Versioning;
using System.Security;
using Microsoft.Win32;

namespace Chaos.Host.Configuration;

/// <summary>
/// Where an installed copy of the product keeps its operator files, as
/// discovered at startup — or the honest statement that this is not an
/// installed copy.
/// </summary>
/// <remarks>
/// <para>
/// The installer writes <c>%ProgramData%\Project CHAOS\config\chaos.host.json</c>
/// and <c>homestead.env</c>, creates <c>db\</c> and <c>logs\</c> beside them,
/// and records every path under <c>HKLM\SOFTWARE\Project CHAOS</c>. This type
/// is the gateway's reading of that layout: which files to load, where the
/// database lives by default, and where logs go. A development checkout
/// resolves to <see cref="Installed"/> = false and the gateway then runs from
/// <c>appsettings.json</c> and environment variables alone.
/// </para>
/// <para>
/// Everything here is a path the gateway <em>will try</em>; nothing is created
/// and nothing is required to exist. <see cref="Searched"/> says where it
/// looked, and <c>/host/info</c> reports it, so "why is my config file being
/// ignored" has an answer without a debugger.
/// </para>
/// </remarks>
public sealed record InstalledLayout
{
    /// <summary>The product's ProgramData folder name, matching the installer.</summary>
    public const string ProductFolderName = "Project CHAOS";

    /// <summary>The gateway configuration file the installer ships.</summary>
    public const string HostConfigurationFileName = "chaos.host.json";

    /// <summary>The backend overrides file the installer ships.</summary>
    public const string BackendEnvironmentFileName = "homestead.env";

    /// <summary>The SQLite file name under the ProgramData <c>db</c> folder.</summary>
    public const string DatabaseFileName = "homestead.db";

    /// <summary>The installer's registry key, relative to <c>HKEY_LOCAL_MACHINE</c>.</summary>
    public const string RegistryKeyPath = @"SOFTWARE\" + ProductFolderName;

    /// <summary>A layout for a machine with no installed copy.</summary>
    public static readonly InstalledLayout NotInstalled = new()
    {
        Source = "none",
        Searched = [],
    };

    /// <summary>
    /// How the layout was found: <c>configuration</c> (<c>Chaos:ConfigDirectory</c>),
    /// <c>registry</c> (<c>HKLM\SOFTWARE\Project CHAOS</c>), <c>programdata</c>
    /// (the default folder existed) or <c>none</c>.
    /// </summary>
    public required string Source { get; init; }

    /// <summary>Every location considered, in order, with the verdict on each.</summary>
    public required IReadOnlyList<string> Searched { get; init; }

    /// <summary>The <c>config</c> directory, or null when not installed.</summary>
    public string? ConfigDirectory { get; init; }

    /// <summary>The ProgramData root (<c>config</c>, <c>db</c>, <c>logs</c> live under it), or null.</summary>
    public string? OperatorDataDirectory { get; init; }

    /// <summary>Where log files go, or null when there is nowhere installed to put them.</summary>
    public string? LogDirectory { get; init; }

    /// <summary>The install root (<c>Chaos.Host.exe</c>, <c>python\</c>, <c>app\</c>), when the registry says.</summary>
    public string? InstallDirectory { get; init; }

    /// <summary>Whether an installed layout was found.</summary>
    public bool Installed => ConfigDirectory is not null;

    /// <summary>Full path of <c>chaos.host.json</c>, or null when not installed. May not exist.</summary>
    public string? HostConfigurationFile =>
        ConfigDirectory is null ? null : Path.Combine(ConfigDirectory, HostConfigurationFileName);

    /// <summary>Full path of <c>homestead.env</c>, or null when not installed. May not exist.</summary>
    public string? BackendEnvironmentFile =>
        ConfigDirectory is null ? null : Path.Combine(ConfigDirectory, BackendEnvironmentFileName);

    /// <summary>
    /// The database the packaged product uses when nothing names one:
    /// <c>sqlite:///&lt;ProgramData&gt;/Project CHAOS/db/homestead.db</c>, written
    /// with forward slashes because it is a SQLAlchemy URL and not a path. This
    /// is the same file <c>chaos.cmd</c> uses, so the service and the command
    /// line never end up with two databases.
    /// </summary>
    public string? DefaultDatabaseUrl =>
        OperatorDataDirectory is null
            ? null
            : "sqlite:///" + Path.Combine(OperatorDataDirectory, "db", DatabaseFileName).Replace('\\', '/');

    /// <summary>
    /// The design package the packaged product loads, <c>&lt;install root&gt;\app\data</c>,
    /// when the install root is known.
    /// </summary>
    public string? DefaultDataDirectory =>
        InstallDirectory is null ? null : Path.Combine(InstallDirectory, "app", "data");
}

/// <summary>Reads the installer's registry key. Faked off Windows and in tests.</summary>
public interface IInstallRegistry
{
    /// <summary>A string value under the product key, or null when absent or unreadable.</summary>
    string? Read(string valueName);
}

/// <summary>The registry on a machine that has none, or where the key is absent.</summary>
public sealed class NullInstallRegistry : IInstallRegistry
{
    /// <summary>The shared instance.</summary>
    public static NullInstallRegistry Instance { get; } = new();

    /// <inheritdoc/>
    public string? Read(string valueName) => null;
}

/// <summary><c>HKLM\SOFTWARE\Project CHAOS</c>, as the installer writes it.</summary>
[SupportedOSPlatform("windows")]
public sealed class WindowsInstallRegistry : IInstallRegistry
{
    /// <inheritdoc/>
    public string? Read(string valueName)
    {
        try
        {
            using var key = Registry.LocalMachine.OpenSubKey(InstalledLayout.RegistryKeyPath, writable: false);
            return key?.GetValue(valueName) as string;
        }
        catch (Exception ex) when (ex is SecurityException or UnauthorizedAccessException or IOException)
        {
            // Unreadable is the same as absent for discovery purposes; the
            // ProgramData default is tried next and the search trail says so.
            return null;
        }
    }
}

/// <summary>Finds the installed layout, in a fixed and reported order.</summary>
public static class InstalledLayoutResolver
{
    /// <summary>
    /// Resolves the layout. <paramref name="configuredConfigDirectory"/> wins
    /// outright (<c>Chaos:ConfigDirectory</c> / <c>CHAOS_ConfigDirectory</c>),
    /// then the registry, then <c>%ProgramData%\Project CHAOS\config</c> if it
    /// exists.
    /// </summary>
    /// <param name="configuredConfigDirectory">An explicit config directory, or null.</param>
    /// <param name="configuredLogDirectory">An explicit log directory, or null; it overrides whatever the layout says.</param>
    /// <param name="registry">The installer's registry key.</param>
    /// <param name="programDataRoot">
    /// The machine's ProgramData folder, or null where the platform has none.
    /// Injectable so the default-folder rule is testable with a temp directory.
    /// </param>
    /// <returns>The layout, never null.</returns>
    public static InstalledLayout Resolve(
        string? configuredConfigDirectory,
        string? configuredLogDirectory,
        IInstallRegistry registry,
        string? programDataRoot)
    {
        ArgumentNullException.ThrowIfNull(registry);

        var searched = new List<string>();
        var layout = ResolveCore(configuredConfigDirectory, registry, programDataRoot, searched);

        if (!string.IsNullOrWhiteSpace(configuredLogDirectory))
        {
            searched.Add($"logs: Chaos:LogDirectory is set to '{configuredLogDirectory}'");
            layout = layout with { LogDirectory = Path.GetFullPath(configuredLogDirectory) };
        }

        return layout with { Searched = searched };
    }

    /// <summary>The registry to use on this machine.</summary>
    /// <returns>The real key on Windows; a null registry elsewhere.</returns>
    public static IInstallRegistry PlatformRegistry() =>
        OperatingSystem.IsWindows() ? new WindowsInstallRegistry() : NullInstallRegistry.Instance;

    /// <summary>The machine's ProgramData folder, or null when it has none.</summary>
    /// <returns>An absolute path, or null.</returns>
    public static string? PlatformProgramData()
    {
        var root = Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData);
        return string.IsNullOrWhiteSpace(root) ? null : root;
    }

    private static InstalledLayout ResolveCore(
        string? configuredConfigDirectory,
        IInstallRegistry registry,
        string? programDataRoot,
        List<string> searched)
    {
        if (!string.IsNullOrWhiteSpace(configuredConfigDirectory))
        {
            var configDirectory = Path.GetFullPath(configuredConfigDirectory);
            var root = Path.GetDirectoryName(configDirectory.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar));
            searched.Add($"configuration: Chaos:ConfigDirectory is set to '{configDirectory}'");

            return new InstalledLayout
            {
                Source = "configuration",
                Searched = searched,
                ConfigDirectory = configDirectory,
                OperatorDataDirectory = root,
                LogDirectory = FirstDirectory(registry.Read("LogDir")) ?? (root is null ? null : Path.Combine(root, "logs")),
                InstallDirectory = FirstDirectory(registry.Read("InstallDir")),
            };
        }

        var registryConfig = FirstDirectory(registry.Read("ConfigDir"));
        if (registryConfig is not null)
        {
            searched.Add($"registry: HKLM\\{InstalledLayout.RegistryKeyPath} ConfigDir = '{registryConfig}'");
            var root = FirstDirectory(registry.Read("DataDir"))
                       ?? Path.GetDirectoryName(registryConfig.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar));

            return new InstalledLayout
            {
                Source = "registry",
                Searched = searched,
                ConfigDirectory = registryConfig,
                OperatorDataDirectory = root,
                LogDirectory = FirstDirectory(registry.Read("LogDir")) ?? (root is null ? null : Path.Combine(root, "logs")),
                InstallDirectory = FirstDirectory(registry.Read("InstallDir")),
            };
        }

        searched.Add(OperatingSystem.IsWindows()
            ? $"registry: HKLM\\{InstalledLayout.RegistryKeyPath} has no ConfigDir value (not installed, or not readable)"
            : "registry: not Windows, so there is no installer key to read");

        if (!string.IsNullOrWhiteSpace(programDataRoot))
        {
            var root = Path.Combine(programDataRoot, InstalledLayout.ProductFolderName);
            var configDirectory = Path.Combine(root, "config");
            if (Directory.Exists(configDirectory))
            {
                searched.Add($"programdata: found '{configDirectory}'");
                return new InstalledLayout
                {
                    Source = "programdata",
                    Searched = searched,
                    ConfigDirectory = configDirectory,
                    OperatorDataDirectory = root,
                    LogDirectory = Path.Combine(root, "logs"),
                };
            }

            searched.Add($"programdata: no '{configDirectory}' directory");
        }
        else
        {
            searched.Add("programdata: this machine reports no ProgramData folder");
        }

        return InstalledLayout.NotInstalled with { Searched = searched };
    }

    private static string? FirstDirectory(string? value)
    {
        if (string.IsNullOrWhiteSpace(value))
        {
            return null;
        }

        var trimmed = value.Trim().TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
        if (trimmed.Length == 0)
        {
            return null;
        }

        try
        {
            return Path.GetFullPath(trimmed);
        }
        catch (Exception ex) when (ex is ArgumentException or NotSupportedException or PathTooLongException)
        {
            // A registry value that is not a path is treated as absent; the
            // search trail still names the source that was tried.
            return null;
        }
    }
}
