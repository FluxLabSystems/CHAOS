using System.Net;
using Chaos.Api;
using Chaos.Host.Routing;
using Chaos.Host.Tests.Support;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Options;

namespace Chaos.Host.Tests;

/// <summary>
/// The ported endpoints are wired so that flipping a route to .NET is a
/// configuration change that works, not one that throws on the first request.
/// </summary>
public sealed class ChaosApiWiringTests
{
    [Fact]
    public void The_sqlite_file_is_derived_from_the_platforms_database_url()
    {
        using var factory = ChaosHostFactory.For()
            .With("DatabaseUrl", "sqlite:////srv/chaos/homestead.db")
            .Build();

        var options = factory.Services.GetRequiredService<IOptions<ChaosApiOptions>>().Value;
        Assert.Equal("/srv/chaos/homestead.db", options.SqliteDatabasePath);

        var resolution = factory.Services.GetRequiredService<ChaosApiDatabaseResolution>();
        Assert.True(resolution.Available);
        Assert.Equal("derived from the platform database URL", resolution.Source);
    }

    [Fact]
    public void An_explicit_api_section_wins_and_binds_from_chaos_api()
    {
        using var factory = ChaosHostFactory.For()
            .With("DatabaseUrl", "sqlite:////srv/chaos/homestead.db")
            .With("Api:SqliteDatabasePath", "/elsewhere/readonly-copy.db")
            .Build();

        var options = factory.Services.GetRequiredService<IOptions<ChaosApiOptions>>().Value;
        Assert.Equal("/elsewhere/readonly-copy.db", options.SqliteDatabasePath);
        Assert.Equal("Chaos:Api:SqliteDatabasePath", factory.Services.GetRequiredService<ChaosApiDatabaseResolution>().Source);
    }

    [Fact]
    public async Task A_non_sqlite_database_leaves_the_endpoints_without_one_and_says_so()
    {
        await using var backend = await StubBackend.StartAsync();
        using var factory = ChaosHostFactory.For(backend)
            .With("DatabaseUrl", "postgresql+psycopg://chaos:secret@db/homestead")
            .Build();
        using var client = factory.CreateClient();

        // The host starts: nothing is owned by Chaos.Api, so nothing needs the file.
        var storage = (await (await client.GetAsync("/host/info")).ReadJsonAsync()).GetProperty("storage");
        Assert.Equal(System.Text.Json.JsonValueKind.Null, storage.GetProperty("chaosApiSqlitePath").ValueKind);
        Assert.Contains("'postgresql+psycopg'", storage.GetStringProperty("chaosApiProblem"), StringComparison.Ordinal);

        // The URL is reported, the password in it is not.
        Assert.Equal("postgresql+psycopg://chaos:***@db/homestead", storage.GetStringProperty("databaseUrl"));
        Assert.DoesNotContain("secret", storage.GetRawText(), StringComparison.Ordinal);
    }

    [Fact]
    public void Owning_a_ported_route_without_a_sqlite_database_stops_the_host()
    {
        using var factory = ChaosHostFactory.For()
            .With("DatabaseUrl", "postgresql://db/homestead")
            .WithRoute(ChaosApiRoutes.Annunciator, RouteOwner.Dotnet, portedInVersion: "0.6.0")
            .Build();

        var thrown = Record.Exception(() => factory.CreateClient());

        Assert.NotNull(thrown);
        var ownership = thrown.Find<RouteOwnershipException>();
        Assert.NotNull(ownership);
        Assert.Contains(ChaosApiRoutes.Annunciator, ownership.Message, StringComparison.Ordinal);
        Assert.Contains("'postgresql'", ownership.Message, StringComparison.Ordinal);
    }

    [Fact]
    public async Task Owning_a_ported_route_with_a_sqlite_database_is_a_configuration_change()
    {
        var directory = Directory.CreateTempSubdirectory("chaos-api-wiring-").FullName;
        try
        {
            using var factory = ChaosHostFactory.For()
                .With("DatabaseUrl", "sqlite:///" + Path.Combine(directory, "homestead.db").Replace('\\', '/'))
                .WithRoute(ChaosApiRoutes.Annunciator, RouteOwner.Dotnet, portedInVersion: "0.6.0")
                .Build();
            using var client = factory.CreateClient();

            var routes = await (await client.GetAsync("/host/routes")).ReadJsonAsync();
            var row = routes.GetProperty("routes").EnumerateArray()
                .Single(entry => entry.GetStringProperty("pathPrefix") == ChaosApiRoutes.Annunciator);
            Assert.Equal("Dotnet", row.GetStringProperty("owner"));
            Assert.Contains(ChaosApiRoutes.Annunciator, row.GetProperty("nativeEndpoints").EnumerateArray().Select(e => e.GetString()));

            // Served in process, not proxied: the response is labelled dotnet
            // even though the file does not exist yet.
            using var response = await client.GetAsync(ChaosApiRoutes.Annunciator);
            Assert.Equal("dotnet", response.Headers.GetValues("X-Chaos-Route-Owner").Single());
            Assert.NotEqual(HttpStatusCode.NotFound, response.StatusCode);
        }
        finally
        {
            Directory.Delete(directory, recursive: true);
        }
    }

    [Theory]
    [InlineData("/api/v1/plugins/mirror", "/api/v1/plugins")]
    [InlineData("/api/v1/ext/netbotz/readings", "/api/v1/ext")]
    [InlineData("/api/v1/rack", "/api/v1/rack")]
    [InlineData("/api/v1/topology/impact/x", "/api/v1/topology")]
    public void Every_python_router_has_its_own_manifest_row(string path, string expectedPrefix)
    {
        var table = RouteOwnershipTable.Build(RouteOwnershipDefaults.Manifest);
        var entry = table.Match(path);

        Assert.NotNull(entry);
        Assert.Equal(expectedPrefix, entry.PathPrefix);
        Assert.Equal(RouteOwner.Python, entry.Owner);
    }
}
