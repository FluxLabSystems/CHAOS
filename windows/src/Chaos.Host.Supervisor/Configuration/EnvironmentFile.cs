using System.Collections.ObjectModel;

namespace Chaos.Host.Supervisor.Configuration;

/// <summary>
/// Reads the installer's <c>homestead.env</c>: <c>KEY=VALUE</c> lines that are
/// handed to the Python child process verbatim.
/// </summary>
/// <remarks>
/// <para>
/// The Service Control Manager hands a service the environment block it
/// inherited at boot, so an installer cannot set a machine variable that a
/// service started in the same session will see. This file is how an operator
/// changes one platform setting on one node without that problem: the
/// supervisor reads it at startup and passes the values to the backend and to
/// first-run setup.
/// </para>
/// <para>
/// The grammar is deliberately the dotenv subset people actually write: blank
/// lines and <c>#</c> comments are ignored, an optional <c>export</c> prefix is
/// tolerated, surrounding single or double quotes are stripped, and everything
/// else is taken literally — no variable expansion, no escapes. A line with no
/// <c>=</c> is reported, not guessed at.
/// </para>
/// </remarks>
public static class EnvironmentFile
{
    /// <summary>Parses the lines of an env file.</summary>
    /// <param name="lines">The file's lines.</param>
    /// <param name="problems">Lines that could not be read, with their line number.</param>
    /// <returns>The variables, in file order, last definition winning.</returns>
    public static IReadOnlyDictionary<string, string> Parse(IEnumerable<string> lines, out IReadOnlyList<string> problems)
    {
        ArgumentNullException.ThrowIfNull(lines);

        var values = new Dictionary<string, string>(StringComparer.Ordinal);
        var found = new List<string>();
        var number = 0;

        foreach (var raw in lines)
        {
            number++;
            var line = raw.Trim();
            if (line.Length == 0 || line.StartsWith('#'))
            {
                continue;
            }

            if (line.StartsWith("export ", StringComparison.Ordinal))
            {
                line = line["export ".Length..].TrimStart();
            }

            var separator = line.IndexOf('=');
            if (separator <= 0)
            {
                found.Add($"line {number}: expected KEY=VALUE, got '{raw.Trim()}'");
                continue;
            }

            var key = line[..separator].Trim();
            if (!IsValidKey(key))
            {
                found.Add($"line {number}: '{key}' is not a valid variable name");
                continue;
            }

            values[key] = Unquote(line[(separator + 1)..].Trim());
        }

        problems = new ReadOnlyCollection<string>(found);
        return new ReadOnlyDictionary<string, string>(values);
    }

    /// <summary>
    /// Reads and parses a file. A missing or unreadable file yields no
    /// variables and one problem, never an exception: the file is optional and
    /// a service must not fail to start over it.
    /// </summary>
    /// <param name="path">The file.</param>
    /// <param name="problems">Why nothing or less than everything was read.</param>
    /// <returns>The variables.</returns>
    public static IReadOnlyDictionary<string, string> Read(string? path, out IReadOnlyList<string> problems)
    {
        if (string.IsNullOrWhiteSpace(path))
        {
            problems = [];
            return new ReadOnlyDictionary<string, string>(new Dictionary<string, string>(StringComparer.Ordinal));
        }

        if (!File.Exists(path))
        {
            problems = [$"'{path}' does not exist"];
            return new ReadOnlyDictionary<string, string>(new Dictionary<string, string>(StringComparer.Ordinal));
        }

        string[] lines;
        try
        {
            lines = File.ReadAllLines(path);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            problems = [$"'{path}' could not be read: {ex.GetType().Name}: {ex.Message}"];
            return new ReadOnlyDictionary<string, string>(new Dictionary<string, string>(StringComparer.Ordinal));
        }

        return Parse(lines, out problems);
    }

    private static bool IsValidKey(string key)
    {
        if (key.Length == 0 || (!char.IsLetter(key[0]) && key[0] != '_'))
        {
            return false;
        }

        foreach (var c in key)
        {
            if (!char.IsLetterOrDigit(c) && c != '_')
            {
                return false;
            }
        }

        return true;
    }

    private static string Unquote(string value)
    {
        if (value.Length >= 2
            && ((value[0] == '"' && value[^1] == '"') || (value[0] == '\'' && value[^1] == '\'')))
        {
            return value[1..^1];
        }

        return value;
    }
}
