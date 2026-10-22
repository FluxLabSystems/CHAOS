using Chaos.Host.Supervisor.Configuration;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.Configuration.EnvironmentVariables;
using Microsoft.Extensions.Configuration.Json;

namespace Chaos.Host.Configuration;

/// <summary>
/// What the gateway read from the installed layout at startup, for
/// <c>GET /host/info</c>.
/// </summary>
/// <remarks>
/// Variable <em>names</em> only from <c>homestead.env</c>, never values: that
/// file is where an operator puts the MQTT password.
/// </remarks>
public sealed record InstalledConfigurationState
{
    /// <summary>The discovered layout.</summary>
    public required InstalledLayout Layout { get; init; }

    /// <summary>Whether <c>chaos.host.json</c> existed and was added as a configuration source.</summary>
    public required bool HostConfigurationFileLoaded { get; init; }

    /// <summary>The variables <c>homestead.env</c> defines, by name.</summary>
    public required IReadOnlyList<string> BackendEnvironmentVariables { get; init; }

    /// <summary>Lines of <c>homestead.env</c> that could not be read, or why the file was not.</summary>
    public required IReadOnlyList<string> BackendEnvironmentProblems { get; init; }
}

/// <summary>Loads the installed layout's files into the host's configuration.</summary>
internal static class InstalledConfiguration
{
    /// <summary>
    /// The gateway's own view of configuration: the <c>Chaos</c> section,
    /// overridden by <c>CHAOS_</c>-prefixed environment variables at the same
    /// level. <c>CHAOS_BackendUrl</c> overrides <c>Chaos:BackendUrl</c>,
    /// <c>CHAOS_Backend__DatabaseUrl</c> overrides <c>Chaos:Backend:DatabaseUrl</c>.
    /// </summary>
    /// <param name="host">The host's configuration.</param>
    /// <returns>A configuration rooted at the section.</returns>
    public static IConfigurationRoot BuildChaosConfiguration(IConfiguration host) =>
        new ConfigurationBuilder()
            .AddConfiguration(host.GetSection(ChaosHostOptions.SectionName))
            .AddEnvironmentVariables(ChaosHostOptions.EnvironmentPrefix)
            .Build();

    /// <summary>
    /// Adds the layout's <c>chaos.host.json</c> as a configuration source,
    /// placed after the shipped <c>appsettings*.json</c> and before the
    /// environment-variable sources so that an operator's edit beats the
    /// shipped defaults and a <c>CHAOS_</c> variable still beats the file.
    /// </summary>
    /// <param name="configuration">The host's configuration builder.</param>
    /// <param name="layout">The discovered layout.</param>
    /// <returns>True when the file exists and was added.</returns>
    public static bool AddHostConfigurationFile(IConfigurationBuilder configuration, InstalledLayout layout)
    {
        ArgumentNullException.ThrowIfNull(configuration);
        ArgumentNullException.ThrowIfNull(layout);

        var path = layout.HostConfigurationFile;
        if (path is null || !File.Exists(path))
        {
            return false;
        }

        // The service is restarted after an edit (and the shell says so), so
        // the file is read once. Watching it would mean a half-written save
        // could rebind the backend address under a running proxy.
        var source = new JsonConfigurationSource
        {
            Path = path,
            Optional = true,
            ReloadOnChange = false,
        };
        source.ResolveFileProvider();

        // The host's chain holds two kinds of environment source: the
        // prefixed ones (ASPNETCORE_, DOTNET_) that configure the host itself
        // and sit BEFORE appsettings.json, and the unprefixed application one
        // that sits after it. The file goes in front of the latter; failing
        // that, straight after the last JSON file; failing that, at the end.
        var sources = configuration.Sources;
        var index = -1;
        for (var i = 0; i < sources.Count; i++)
        {
            if (sources[i] is EnvironmentVariablesConfigurationSource { Prefix: null or "" })
            {
                index = i;
                break;
            }
        }

        if (index < 0)
        {
            for (var i = sources.Count - 1; i >= 0; i--)
            {
                if (sources[i] is JsonConfigurationSource)
                {
                    index = i + 1;
                    break;
                }
            }
        }

        if (index < 0 || index >= sources.Count)
        {
            sources.Add(source);
        }
        else
        {
            sources.Insert(index, source);
        }

        return true;
    }

    /// <summary>Reads <c>homestead.env</c> and describes what was read.</summary>
    /// <param name="layout">The discovered layout.</param>
    /// <param name="hostConfigurationFileLoaded">Whether <c>chaos.host.json</c> was loaded.</param>
    /// <param name="variables">The variables, for the supervisor and setup.</param>
    /// <returns>The report for <c>/host/info</c>.</returns>
    public static InstalledConfigurationState ReadBackendEnvironment(
        InstalledLayout layout,
        bool hostConfigurationFileLoaded,
        out IReadOnlyDictionary<string, string> variables)
    {
        ArgumentNullException.ThrowIfNull(layout);

        variables = EnvironmentFile.Read(layout.BackendEnvironmentFile, out var problems);

        return new InstalledConfigurationState
        {
            Layout = layout,
            HostConfigurationFileLoaded = hostConfigurationFileLoaded,
            BackendEnvironmentVariables = [.. variables.Keys.Order(StringComparer.Ordinal)],
            BackendEnvironmentProblems = problems,
        };
    }
}
