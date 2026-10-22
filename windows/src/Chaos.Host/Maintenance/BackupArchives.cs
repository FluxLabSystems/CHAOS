using System.Text.RegularExpressions;

namespace Chaos.Host.Maintenance;

/// <summary>
/// The archives <c>chaos backup</c> writes, as the gateway reads them back:
/// which files in the backup directory are archives, which one a run just
/// wrote, and which old ones to prune.
/// </summary>
/// <remarks>
/// Everything here keys off the platform CLI's own naming,
/// <c>homestead-&lt;role&gt;-&lt;UTC stamp&gt;.tar.gz</c>
/// (<c>cmd_backup</c> in <c>src/chaos/cli.py</c>). A file in the directory that
/// is not named that way - an operator's own copy, a restore staging file, a
/// note - is never counted and never deleted, whatever its extension.
/// </remarks>
internal static partial class BackupArchives
{
    /// <summary>What pruning did, and what it could not do.</summary>
    /// <param name="Deleted">Full paths removed, oldest first.</param>
    /// <param name="Kept">Full paths that remain, oldest first.</param>
    /// <param name="Problems">Deletions that failed, with the reason; the file is still there.</param>
    public sealed record PruneResult(IReadOnlyList<string> Deleted, IReadOnlyList<string> Kept, IReadOnlyList<string> Problems);

    /// <summary>Whether <paramref name="fileName"/> is an archive the platform CLI would have written.</summary>
    public static bool IsArchive(string fileName) => ArchiveName().IsMatch(fileName);

    /// <summary>The UTC stamp in an archive's name, or null when it is not an archive.</summary>
    public static string? StampOf(string fileName)
    {
        var match = ArchiveName().Match(fileName);
        return match.Success ? match.Groups["stamp"].Value : null;
    }

    /// <summary>
    /// The archive path the CLI printed (<c>Backup   : &lt;path&gt;</c>), or
    /// null when the output did not carry one.
    /// </summary>
    public static string? ArchivePathFrom(string standardOutput)
    {
        var match = BackupLine().Match(standardOutput ?? string.Empty);
        return match.Success ? match.Groups["path"].Value : null;
    }

    /// <summary>The SHA-256 the CLI printed, lower-cased, or null.</summary>
    public static string? DigestFrom(string standardOutput)
    {
        var match = DigestLine().Match(standardOutput ?? string.Empty);
        return match.Success ? match.Groups["digest"].Value.ToLowerInvariant() : null;
    }

    /// <summary>
    /// Deletes the oldest archives in <paramref name="directory"/> until at
    /// most <paramref name="keep"/> remain. Age is the UTC stamp in the name,
    /// not the file's modification time, so a copied-back archive keeps its
    /// place in the sequence.
    /// </summary>
    /// <param name="directory">The backup directory.</param>
    /// <param name="keep">How many to keep; at least one.</param>
    /// <returns>What happened.</returns>
    public static PruneResult Prune(string directory, int keep)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(directory);
        ArgumentOutOfRangeException.ThrowIfLessThan(keep, 1);

        List<string> archives;
        try
        {
            archives = Directory.EnumerateFiles(directory, "homestead-*.tar.gz")
                .Where(path => IsArchive(Path.GetFileName(path)))
                .OrderBy(path => StampOf(Path.GetFileName(path)), StringComparer.Ordinal)
                .ThenBy(path => path, StringComparer.Ordinal)
                .ToList();
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            return new PruneResult([], [], [$"could not list {directory}: {ex.GetType().Name}: {ex.Message}"]);
        }

        var excess = archives.Count - keep;
        if (excess <= 0)
        {
            return new PruneResult([], archives, []);
        }

        var deleted = new List<string>();
        var problems = new List<string>();
        var kept = new List<string>(archives.Skip(excess));

        foreach (var path in archives.Take(excess))
        {
            try
            {
                File.Delete(path);
                deleted.Add(path);
            }
            catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
            {
                problems.Add($"could not delete {path}: {ex.GetType().Name}: {ex.Message}");
                kept.Insert(0, path);
            }
        }

        return new PruneResult(deleted, kept, problems);
    }

    [GeneratedRegex(@"^homestead-(?<role>.+)-(?<stamp>\d{8}T\d{6}Z)\.tar\.gz$", RegexOptions.CultureInvariant)]
    private static partial Regex ArchiveName();

    [GeneratedRegex(@"^Backup\s*:\s*(?<path>.+?)\s*$", RegexOptions.Multiline | RegexOptions.CultureInvariant)]
    private static partial Regex BackupLine();

    [GeneratedRegex(@"^SHA-256\s*:\s*(?<digest>[0-9A-Fa-f]{64})\s*$", RegexOptions.Multiline | RegexOptions.CultureInvariant)]
    private static partial Regex DigestLine();
}
