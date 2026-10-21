namespace Chaos.Host.Configuration;

/// <summary>
/// The one answer to "which database, which design package" that the
/// gateway hands to every consumer: first-run setup, the supervised backend,
/// and the natively served endpoints.
/// </summary>
/// <remarks>
/// <para>
/// There used to be two: setup preferred <c>Chaos:DatabaseUrl</c> while the
/// supervisor read only <c>Chaos:Backend:DatabaseUrl</c>, so a node could set
/// up one database and serve another. Resolution now happens once, here, and
/// both options objects are overwritten with the result.
/// </para>
/// <para>
/// Precedence, highest first: the gateway's own setting (<c>Chaos:DatabaseUrl</c>
/// / <c>CHAOS_DatabaseUrl</c>), the supervisor section
/// (<c>Chaos:Backend:DatabaseUrl</c> / <c>CHAOS_Backend__DatabaseUrl</c>, which
/// is what the desktop shell sets on a child it starts), the installer's
/// <c>homestead.env</c>, then the installed layout's default. Nothing named
/// anywhere means the platform's own default, which the gateway then reports
/// rather than guesses.
/// </para>
/// </remarks>
public sealed record PlatformStorage
{
    /// <summary>The platform's <c>CHAOS_DATABASE_URL</c>, as <c>homestead.env</c> would spell it.</summary>
    public const string DatabaseUrlVariable = "CHAOS_DATABASE_URL";

    /// <summary>The platform's <c>CHAOS_DATA_DIR</c>, as <c>homestead.env</c> would spell it.</summary>
    public const string DataDirectoryVariable = "CHAOS_DATA_DIR";

    /// <summary>The effective SQLAlchemy URL, or null for "the platform's own default".</summary>
    public string? DatabaseUrl { get; init; }

    /// <summary>Where <see cref="DatabaseUrl"/> came from, in words an operator can act on.</summary>
    public required string DatabaseUrlSource { get; init; }

    /// <summary>The effective design-package directory, or null for "the platform's own default".</summary>
    public string? DataDirectory { get; init; }

    /// <summary>Where <see cref="DataDirectory"/> came from.</summary>
    public required string DataDirectorySource { get; init; }

    /// <summary>
    /// Resolves both settings from every place they can be named.
    /// </summary>
    /// <param name="gatewayDatabaseUrl"><c>Chaos:DatabaseUrl</c>.</param>
    /// <param name="gatewayDataDirectory"><c>Chaos:DataDirectory</c>.</param>
    /// <param name="backendDatabaseUrl"><c>Chaos:Backend:DatabaseUrl</c>.</param>
    /// <param name="backendDataDirectory"><c>Chaos:Backend:DataDirectory</c>.</param>
    /// <param name="backendEnvironment">The variables read from <c>homestead.env</c>.</param>
    /// <param name="layout">The installed layout, for its defaults.</param>
    /// <returns>The resolution.</returns>
    public static PlatformStorage Resolve(
        string? gatewayDatabaseUrl,
        string? gatewayDataDirectory,
        string? backendDatabaseUrl,
        string? backendDataDirectory,
        IReadOnlyDictionary<string, string> backendEnvironment,
        InstalledLayout layout)
    {
        ArgumentNullException.ThrowIfNull(backendEnvironment);
        ArgumentNullException.ThrowIfNull(layout);

        var (databaseUrl, databaseSource) = First(
            (gatewayDatabaseUrl, "Chaos:DatabaseUrl"),
            (backendDatabaseUrl, "Chaos:Backend:DatabaseUrl"),
            (backendEnvironment.GetValueOrDefault(DatabaseUrlVariable), $"{InstalledLayout.BackendEnvironmentFileName} ({DatabaseUrlVariable})"),
            (layout.DefaultDatabaseUrl, $"installed layout default ({layout.OperatorDataDirectory})"),
            (null, "platform default (nothing names a database)"));

        var (dataDirectory, dataSource) = First(
            (gatewayDataDirectory, "Chaos:DataDirectory"),
            (backendDataDirectory, "Chaos:Backend:DataDirectory"),
            (backendEnvironment.GetValueOrDefault(DataDirectoryVariable), $"{InstalledLayout.BackendEnvironmentFileName} ({DataDirectoryVariable})"),
            (layout.DefaultDataDirectory, $"installed layout default ({layout.InstallDirectory})"),
            (null, "platform default (nothing names a data directory)"));

        return new PlatformStorage
        {
            DatabaseUrl = databaseUrl,
            DatabaseUrlSource = databaseSource,
            DataDirectory = dataDirectory,
            DataDirectorySource = dataSource,
        };
    }

    private static (string? Value, string Source) First(params (string? Value, string Source)[] candidates)
    {
        foreach (var (value, source) in candidates)
        {
            if (!string.IsNullOrWhiteSpace(value))
            {
                return (value.Trim(), source);
            }
        }

        return candidates[^1];
    }
}
