using Chaos.Api.Data;

namespace Chaos.Api.Tests;

/// <summary>The SQLAlchemy URL the platform is configured with, read as a file path.</summary>
public sealed class SqliteDatabaseUrlTests
{
    [Theory]
    [InlineData("sqlite:///var/homestead.db", "var/homestead.db")]
    [InlineData("sqlite:////srv/chaos/homestead.db", "/srv/chaos/homestead.db")]
    [InlineData("sqlite:///C:/ProgramData/Project CHAOS/db/homestead.db", "C:/ProgramData/Project CHAOS/db/homestead.db")]
    [InlineData("sqlite:///C:/ProgramData/Project%20CHAOS/db/homestead.db", "C:/ProgramData/Project CHAOS/db/homestead.db")]
    [InlineData("sqlite+pysqlite:////srv/chaos/homestead.db", "/srv/chaos/homestead.db")]
    [InlineData("sqlite:////srv/chaos/homestead.db?mode=ro&check_same_thread=false", "/srv/chaos/homestead.db")]
    [InlineData("  sqlite:///relative.db  ", "relative.db")]
    public void Every_sqlalchemy_spelling_yields_the_file(string url, string expected)
    {
        Assert.True(SqliteDatabaseUrl.TryGetPath(url, out var path, out var problem), problem);
        Assert.Equal(expected, path);
        Assert.Null(problem);
    }

    [Theory]
    [InlineData("postgresql+psycopg://chaos:secret@db:5432/homestead", "postgresql+psycopg")]
    [InlineData("postgresql://db/homestead", "postgresql")]
    public void Another_database_is_refused_with_the_scheme_and_the_remedy(string url, string scheme)
    {
        Assert.False(SqliteDatabaseUrl.TryGetPath(url, out var path, out var problem));
        Assert.Null(path);
        Assert.Contains($"'{scheme}'", problem, StringComparison.Ordinal);
        Assert.Contains("IChaosConnectionFactory", problem, StringComparison.Ordinal);

        // Never echo the URL: it carries the password.
        Assert.DoesNotContain("secret", problem, StringComparison.Ordinal);
    }

    [Theory]
    [InlineData("sqlite://")]
    [InlineData("sqlite:///:memory:")]
    public void An_in_memory_database_is_refused(string url)
    {
        Assert.False(SqliteDatabaseUrl.TryGetPath(url, out _, out var problem));
        Assert.Contains("in-memory", problem, StringComparison.Ordinal);
    }

    [Theory]
    [InlineData(null)]
    [InlineData("")]
    [InlineData("   ")]
    public void Nothing_configured_says_so(string? url)
    {
        Assert.False(SqliteDatabaseUrl.TryGetPath(url, out _, out var problem));
        Assert.Equal("no database URL is configured", problem);
    }

    [Theory]
    [InlineData("homestead.db")]
    [InlineData("sqlite://host/file.db")]
    public void A_path_or_a_host_url_is_not_mistaken_for_a_file_url(string url)
    {
        Assert.False(SqliteDatabaseUrl.TryGetPath(url, out _, out var problem));
        Assert.NotNull(problem);
    }
}
