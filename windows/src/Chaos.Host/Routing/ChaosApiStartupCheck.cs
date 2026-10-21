using Chaos.Api;
using Chaos.Api.Data;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace Chaos.Host.Routing;

/// <summary>
/// What the gateway worked out about the database the natively served
/// endpoints would read, for <c>/host/info</c> and the startup check.
/// </summary>
/// <param name="SqlitePath">The SQLite file the ported endpoints open read-only, or null.</param>
/// <param name="Source">Where <paramref name="SqlitePath"/> came from.</param>
/// <param name="Problem">Why there is no path, or null.</param>
public sealed record ChaosApiDatabaseResolution(string? SqlitePath, string Source, string? Problem)
{
    /// <summary>Whether the ported endpoints have a database to read.</summary>
    public bool Available => SqlitePath is not null;

    /// <summary>
    /// Resolves the path: an explicit <c>Chaos:Api:SqliteDatabasePath</c>
    /// wins, otherwise the platform's database URL when it names a SQLite file.
    /// </summary>
    /// <param name="configuredPath"><c>Chaos:Api:SqliteDatabasePath</c>.</param>
    /// <param name="databaseUrl">The platform's effective database URL.</param>
    /// <returns>The resolution.</returns>
    public static ChaosApiDatabaseResolution Resolve(string? configuredPath, string? databaseUrl)
    {
        if (!string.IsNullOrWhiteSpace(configuredPath))
        {
            return new ChaosApiDatabaseResolution(configuredPath.Trim(), "Chaos:Api:SqliteDatabasePath", null);
        }

        return SqliteDatabaseUrl.TryGetPath(databaseUrl, out var path, out var problem)
            ? new ChaosApiDatabaseResolution(path, "derived from the platform database URL", null)
            : new ChaosApiDatabaseResolution(null, "none", problem);
    }
}

/// <summary>
/// Refuses to start a host that owns a <c>Chaos.Api</c> route natively but
/// has no SQLite database for it to read.
/// </summary>
/// <remarks>
/// Runs after <see cref="RouteOwnershipStartupCheck"/>, for the same reason it
/// exists: a route marked ported with nothing behind it answers an error, and
/// an error from the annunciator endpoint reads like "no alarms". The file is
/// not required to exist yet — first-run setup creates it — only to be
/// nameable.
/// </remarks>
internal sealed class ChaosApiStartupCheck : IHostedService
{
    private readonly RouteOwnershipTable _table;
    private readonly ChaosApiDatabaseResolution _database;
    private readonly ILogger<ChaosApiStartupCheck> _logger;

    public ChaosApiStartupCheck(
        RouteOwnershipTable table,
        ChaosApiDatabaseResolution database,
        ILogger<ChaosApiStartupCheck> logger)
    {
        _table = table;
        _database = database;
        _logger = logger;
    }

    public Task StartAsync(CancellationToken cancellationToken)
    {
        var owned = ChaosApiRoutes.OwnedPrefixes
            .Where(prefix => _table.Match(prefix)?.Owner == RouteOwner.Dotnet)
            .ToList();

        if (owned.Count == 0)
        {
            return Task.CompletedTask;
        }

        if (!_database.Available)
        {
            throw new RouteOwnershipException(
                $"The route-ownership manifest marks {string.Join(", ", owned)} as served by Chaos.Api, but the "
              + $"ported endpoints have no SQLite database to read: {_database.Problem}. Point Chaos:DatabaseUrl at a "
              + "sqlite:/// file, set Chaos:Api:SqliteDatabasePath, or move the row back to Python.");
        }

        if (!File.Exists(_database.SqlitePath))
        {
            _logger.LogWarning(
                "Chaos.Api serves {Routes} from {Path}, which does not exist yet. The endpoints will answer errors "
              + "until the platform (or first-run setup) creates it.",
                string.Join(", ", owned),
                _database.SqlitePath);
        }

        return Task.CompletedTask;
    }

    public Task StopAsync(CancellationToken cancellationToken) => Task.CompletedTask;
}
