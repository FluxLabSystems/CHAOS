using System.Globalization;
using System.Text;
using Microsoft.Extensions.Logging;

namespace Chaos.Host.Supervisor.Logging;

/// <summary>Where and how big the gateway's log files are.</summary>
public sealed class RollingFileLoggerOptions
{
    /// <summary>The directory the files live in. Created on first write if absent.</summary>
    public required string Directory { get; init; }

    /// <summary>
    /// The current file's name. Rotated copies take a number before the
    /// extension: <c>chaos-host.log</c>, <c>chaos-host.1.log</c> (the newest
    /// rotated), <c>chaos-host.2.log</c> …
    /// </summary>
    public string FileName { get; init; } = "chaos-host.log";

    /// <summary>Size at which the current file is rotated. 8 MiB by default.</summary>
    public long MaxFileSizeBytes { get; init; } = 8 * 1024 * 1024;

    /// <summary>
    /// Rotated files kept beside the current one. Ten by default: with the
    /// size above that is roughly 90 MiB of history, which is weeks of a quiet
    /// node and the whole of a bad night.
    /// </summary>
    public int MaxRetainedFiles { get; init; } = 10;

    /// <summary>Entries below this level are not written. The host's filters apply on top.</summary>
    public LogLevel MinimumLevel { get; init; } = LogLevel.Information;
}

/// <summary>
/// Writes log entries to size-rotated files, for a service nobody is
/// watching the console of.
/// </summary>
/// <remarks>
/// <para>
/// Deliberately small and dependency-free: one file open in append mode and
/// shared for reading so an operator can tail it, one lock, a size check
/// before every write, and rotation by renaming. Everything the host logs
/// goes through it — the supervisor's lifecycle, the backend's own stdout and
/// stderr (relayed as <c>[backend:stderr]</c> lines), first-run setup, the
/// proxy — so the folder the shell's <b>Open logs</b> button opens has the
/// whole story in one place.
/// </para>
/// <para>
/// It never throws into the caller. A full or read-only disk is recorded in
/// <see cref="Problem"/>, writes are dropped for a while, and the next attempt
/// is made later; logging must not be able to take the control plane down.
/// </para>
/// </remarks>
[ProviderAlias("File")]
public sealed class RollingFileLoggerProvider : ILoggerProvider
{
    private readonly RollingFileSink _sink;
    private readonly LogLevel _minimumLevel;

    public RollingFileLoggerProvider(RollingFileLoggerOptions options)
    {
        ArgumentNullException.ThrowIfNull(options);
        _sink = new RollingFileSink(options);
        _minimumLevel = options.MinimumLevel;
    }

    /// <summary>The current file's full path.</summary>
    public string CurrentFile => _sink.CurrentFile;

    /// <summary>Why writing last failed, or null while it is working.</summary>
    public string? Problem => _sink.Problem;

    public ILogger CreateLogger(string categoryName) => new RollingFileLogger(_sink, categoryName, _minimumLevel);

    public void Dispose() => _sink.Dispose();
}

/// <summary>One line per entry, in a shape a person can read and a tool can grep.</summary>
public static class RollingFileFormatter
{
    /// <summary>
    /// Formats an entry: <c>2026-01-01T00:00:00.000Z [INF] Category[1234] message</c>,
    /// with any exception on indented lines below.
    /// </summary>
    public static string Format(
        DateTimeOffset timestampUtc,
        LogLevel level,
        string category,
        EventId eventId,
        string message,
        Exception? exception)
    {
        var builder = new StringBuilder(128);
        builder.Append(timestampUtc.ToUniversalTime().ToString("yyyy-MM-dd'T'HH:mm:ss.fff'Z'", CultureInfo.InvariantCulture));
        builder.Append(" [").Append(Abbreviate(level)).Append("] ");
        builder.Append(category);
        if (eventId.Id != 0)
        {
            builder.Append('[').Append(eventId.Id.ToString(CultureInfo.InvariantCulture)).Append(']');
        }

        builder.Append(' ').Append(message);

        if (exception is not null)
        {
            foreach (var line in exception.ToString().Split('\n'))
            {
                builder.Append(Environment.NewLine).Append("    ").Append(line.TrimEnd('\r'));
            }
        }

        return builder.ToString();
    }

    /// <summary>The three-letter level tag used in the files.</summary>
    public static string Abbreviate(LogLevel level) => level switch
    {
        LogLevel.Trace => "TRC",
        LogLevel.Debug => "DBG",
        LogLevel.Information => "INF",
        LogLevel.Warning => "WRN",
        LogLevel.Error => "ERR",
        LogLevel.Critical => "CRT",
        _ => "???",
    };
}

internal sealed class RollingFileLogger(RollingFileSink sink, string category, LogLevel minimumLevel) : ILogger
{
    public IDisposable? BeginScope<TState>(TState state)
        where TState : notnull => null;

    public bool IsEnabled(LogLevel logLevel) => logLevel != LogLevel.None && logLevel >= minimumLevel;

    public void Log<TState>(
        LogLevel logLevel,
        EventId eventId,
        TState state,
        Exception? exception,
        Func<TState, Exception?, string> formatter)
    {
        if (!IsEnabled(logLevel))
        {
            return;
        }

        ArgumentNullException.ThrowIfNull(formatter);
        sink.Write(RollingFileFormatter.Format(DateTimeOffset.UtcNow, logLevel, category, eventId, formatter(state, exception), exception));
    }
}

/// <summary>The file, the lock, the size check and the rotation.</summary>
public sealed class RollingFileSink : IDisposable
{
    private static readonly TimeSpan RetryAfterFailure = TimeSpan.FromSeconds(30);

    private readonly Lock _gate = new();
    private readonly RollingFileLoggerOptions _options;
    private readonly string _directory;
    private readonly string _baseName;
    private readonly string _extension;
    private FileStream? _stream;
    private long _length;
    private DateTimeOffset _failedUtc = DateTimeOffset.MinValue;
    private string? _problem;
    private bool _disposed;

    public RollingFileSink(RollingFileLoggerOptions options)
    {
        ArgumentNullException.ThrowIfNull(options);
        ArgumentException.ThrowIfNullOrWhiteSpace(options.Directory);
        ArgumentException.ThrowIfNullOrWhiteSpace(options.FileName);
        ArgumentOutOfRangeException.ThrowIfLessThan(options.MaxFileSizeBytes, 1024);
        ArgumentOutOfRangeException.ThrowIfNegative(options.MaxRetainedFiles);

        _options = options;
        _directory = Path.GetFullPath(options.Directory);
        _baseName = Path.GetFileNameWithoutExtension(options.FileName);
        _extension = Path.GetExtension(options.FileName);
        CurrentFile = Path.Combine(_directory, options.FileName);
    }

    /// <summary>The current file's full path.</summary>
    public string CurrentFile { get; }

    /// <summary>Why writing last failed, or null while it is working.</summary>
    public string? Problem
    {
        get
        {
            lock (_gate)
            {
                return _problem;
            }
        }
    }

    /// <summary>The path a rotated file takes: <c>name.1.log</c>, <c>name.2.log</c> …</summary>
    public string RotatedFile(int index) =>
        Path.Combine(_directory, $"{_baseName}.{index.ToString(CultureInfo.InvariantCulture)}{_extension}");

    /// <summary>Appends one entry (a newline is added), rotating first if it would not fit.</summary>
    public void Write(string entry)
    {
        ArgumentNullException.ThrowIfNull(entry);

        var bytes = Encoding.UTF8.GetBytes(entry + Environment.NewLine);

        lock (_gate)
        {
            if (_disposed)
            {
                return;
            }

            var now = DateTimeOffset.UtcNow;
            if (_problem is not null && now - _failedUtc < RetryAfterFailure)
            {
                return;
            }

            try
            {
                EnsureOpenLocked();

                if (_length > 0 && _length + bytes.Length > _options.MaxFileSizeBytes)
                {
                    RotateLocked();
                    EnsureOpenLocked();
                }

                _stream!.Write(bytes, 0, bytes.Length);
                _stream.Flush();
                _length += bytes.Length;
                _problem = null;
            }
            catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or NotSupportedException)
            {
                _problem = $"Could not write '{CurrentFile}': {ex.GetType().Name}: {ex.Message}";
                _failedUtc = now;
                CloseLocked();
            }
        }
    }

    public void Dispose()
    {
        lock (_gate)
        {
            _disposed = true;
            CloseLocked();
        }
    }

    private void EnsureOpenLocked()
    {
        if (_stream is not null)
        {
            return;
        }

        Directory.CreateDirectory(_directory);

        // Shared for reading and writing so an operator can tail the file and a
        // second writer (there should be none) fails loudly at its write
        // rather than at ours.
        _stream = new FileStream(CurrentFile, FileMode.Append, FileAccess.Write, FileShare.ReadWrite);
        _length = _stream.Length;
    }

    private void CloseLocked()
    {
        _stream?.Dispose();
        _stream = null;
        _length = 0;
    }

    private void RotateLocked()
    {
        CloseLocked();

        if (_options.MaxRetainedFiles == 0)
        {
            File.Delete(CurrentFile);
            return;
        }

        // Oldest first: name.N is deleted, name.N-1 becomes name.N, and so on
        // down to the current file, which becomes name.1.
        var oldest = RotatedFile(_options.MaxRetainedFiles);
        if (File.Exists(oldest))
        {
            File.Delete(oldest);
        }

        for (var index = _options.MaxRetainedFiles - 1; index >= 1; index--)
        {
            var source = RotatedFile(index);
            if (File.Exists(source))
            {
                File.Move(source, RotatedFile(index + 1), overwrite: true);
            }
        }

        if (File.Exists(CurrentFile))
        {
            File.Move(CurrentFile, RotatedFile(1), overwrite: true);
        }
    }
}
