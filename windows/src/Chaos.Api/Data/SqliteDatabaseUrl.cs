namespace Chaos.Api.Data;

/// <summary>
/// Turns the platform's SQLAlchemy database URL into the file path
/// <see cref="SqliteReadOnlyConnectionFactory"/> needs — or says why it cannot.
/// </summary>
/// <remarks>
/// <para>
/// The platform is configured with a URL (<c>CHAOS_DATABASE_URL</c>,
/// <c>Chaos:DatabaseUrl</c>), and the gateway used to hand that URL to this
/// assembly as if it were a path: <c>Path.GetFullPath("sqlite:///…")</c>
/// produced a file that does not exist, and flipping a route to .NET would
/// have thrown on its first request. The three SQLAlchemy spellings are
/// handled here, once:
/// </para>
/// <list type="bullet">
/// <item><c>sqlite:///relative/file.db</c> — relative to the working directory;</item>
/// <item><c>sqlite:////absolute/unix/file.db</c> — four slashes;</item>
/// <item><c>sqlite:///C:/absolute/windows/file.db</c> — a drive letter after three.</item>
/// </list>
/// <para>
/// A driver suffix (<c>sqlite+pysqlite://</c>) is accepted, a query string is
/// dropped, and percent-escapes are decoded. In-memory databases and every
/// other scheme are refused with a sentence that says what to do instead.
/// </para>
/// </remarks>
public static class SqliteDatabaseUrl
{
    /// <summary>
    /// Extracts the file path from <paramref name="url"/>.
    /// </summary>
    /// <param name="url">The SQLAlchemy URL, or null/empty for "none configured".</param>
    /// <param name="path">The file path, when the URL names a SQLite file.</param>
    /// <param name="problem">Why not, otherwise. Null when <paramref name="path"/> is set.</param>
    /// <returns>True when a path was extracted.</returns>
    public static bool TryGetPath(string? url, out string? path, out string? problem)
    {
        path = null;
        problem = null;

        if (string.IsNullOrWhiteSpace(url))
        {
            problem = "no database URL is configured";
            return false;
        }

        var text = url.Trim();
        var separator = text.IndexOf("://", StringComparison.Ordinal);
        if (separator <= 0)
        {
            problem = $"'{text}' is not a database URL (expected scheme://…)";
            return false;
        }

        var scheme = text[..separator];
        if (!scheme.Equals("sqlite", StringComparison.OrdinalIgnoreCase)
            && !scheme.StartsWith("sqlite+", StringComparison.OrdinalIgnoreCase))
        {
            problem = $"the database URL uses the '{scheme}' scheme and Chaos.Api reads SQLite files only. "
                    + "Register an IChaosConnectionFactory for that database before calling AddChaosApi(), "
                    + "or keep the routes this assembly serves owned by the Python backend.";
            return false;
        }

        var rest = text[(separator + 3)..];
        var query = rest.IndexOf('?');
        if (query >= 0)
        {
            rest = rest[..query];
        }

        if (rest.Length == 0)
        {
            problem = "the database URL names an in-memory SQLite database, which another process cannot read";
            return false;
        }

        if (!rest.StartsWith('/'))
        {
            problem = $"the SQLite URL '{text}' has a host component; only file URLs "
                    + "(sqlite:///path, sqlite:////absolute/path, sqlite:///C:/path) are supported";
            return false;
        }

        var file = Uri.UnescapeDataString(rest[1..]);
        if (file.Length == 0 || string.Equals(file, ":memory:", StringComparison.OrdinalIgnoreCase))
        {
            problem = "the database URL names an in-memory SQLite database, which another process cannot read";
            return false;
        }

        path = file;
        return true;
    }
}
