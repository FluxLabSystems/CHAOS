using Chaos.Host.Supervisor.Logging;
using Microsoft.Extensions.Logging;
using Xunit;

namespace Chaos.Host.Supervisor.Tests;

/// <summary>The gateway's log files: their shape, their rotation, and their refusal to throw.</summary>
public sealed class RollingFileLoggerTests : IDisposable
{
    private readonly string _directory = Path.Combine(Path.GetTempPath(), "chaos-file-logger-" + Guid.NewGuid().ToString("N"));

    public void Dispose()
    {
        try
        {
            if (Directory.Exists(_directory))
            {
                Directory.Delete(_directory, recursive: true);
            }
        }
        catch (IOException)
        {
            // A temp directory that will not delete is not a test failure.
        }
    }

    [Fact]
    public void An_entry_is_one_line_a_person_can_read_and_a_tool_can_grep()
    {
        var line = RollingFileFormatter.Format(
            new DateTimeOffset(2026, 10, 21, 3, 4, 5, 678, TimeSpan.FromHours(2)),
            LogLevel.Warning,
            "Chaos.Host.Supervisor.BackendSupervisor",
            new EventId(1022, "BackendExited"),
            "the backend exited with code 1",
            exception: null);

        Assert.Equal("2026-10-21T01:04:05.678Z [WRN] Chaos.Host.Supervisor.BackendSupervisor[1022] the backend exited with code 1", line);
    }

    [Fact]
    public void An_exception_follows_on_indented_lines_and_a_zero_event_id_is_omitted()
    {
        var line = RollingFileFormatter.Format(
            DateTimeOffset.UnixEpoch,
            LogLevel.Critical,
            "Chaos.Host",
            default,
            "it broke",
            new InvalidOperationException("no"));

        var lines = line.Split(Environment.NewLine);
        Assert.Equal("1970-01-01T00:00:00.000Z [CRT] Chaos.Host it broke", lines[0]);
        Assert.StartsWith("    System.InvalidOperationException: no", lines[1], StringComparison.Ordinal);
    }

    [Fact]
    public void Writes_through_a_logger_factory_and_creates_the_directory()
    {
        var directory = Path.Combine(_directory, "logs");
        using var provider = new RollingFileLoggerProvider(new RollingFileLoggerOptions { Directory = directory });
        using var factory = LoggerFactory.Create(logging => logging.AddProvider(provider).SetMinimumLevel(LogLevel.Debug));

        var logger = factory.CreateLogger("Chaos.Host.Tests");
        logger.LogInformation("hello {Who}", "operator");
        logger.LogDebug("not written: below the provider's own minimum");

        var text = File.ReadAllText(Path.Combine(directory, "chaos-host.log"));
        Assert.Contains("[INF] Chaos.Host.Tests hello operator", text, StringComparison.Ordinal);
        Assert.DoesNotContain("not written", text, StringComparison.Ordinal);
        Assert.Null(provider.Problem);
    }

    [Fact]
    public void Rotates_by_size_keeps_the_configured_count_and_drops_the_oldest()
    {
        var options = new RollingFileLoggerOptions
        {
            Directory = _directory,
            FileName = "test.log",
            MaxFileSizeBytes = 1024,
            MaxRetainedFiles = 2,
        };
        using var sink = new RollingFileSink(options);

        // ~100 bytes a line, so every ~10 lines rotate; 60 lines is five rotations.
        for (var i = 0; i < 60; i++)
        {
            sink.Write($"line {i:D3} " + new string('x', 90));
        }

        var files = Directory.GetFiles(_directory).Select(Path.GetFileName).Order(StringComparer.Ordinal).ToList();
        Assert.Equal(["test.1.log", "test.2.log", "test.log"], files);

        // The newest lines are in the current file, older ones in .1, older
        // still in .2, and everything before that is gone.
        Assert.Contains("line 059", File.ReadAllText(sink.CurrentFile), StringComparison.Ordinal);
        var first = File.ReadAllText(sink.RotatedFile(1));
        var second = File.ReadAllText(sink.RotatedFile(2));
        Assert.True(LastLineNumber(second) < LastLineNumber(first));
        Assert.True(LastLineNumber(first) < 59);
        Assert.DoesNotContain("line 000", first + second + File.ReadAllText(sink.CurrentFile), StringComparison.Ordinal);

        Assert.True(new FileInfo(sink.CurrentFile).Length <= options.MaxFileSizeBytes);
        Assert.True(new FileInfo(sink.RotatedFile(1)).Length <= options.MaxFileSizeBytes);
    }

    [Fact]
    public void A_single_oversized_entry_is_still_written()
    {
        using var sink = new RollingFileSink(new RollingFileLoggerOptions
        {
            Directory = _directory,
            FileName = "big.log",
            MaxFileSizeBytes = 1024,
            MaxRetainedFiles = 1,
        });

        sink.Write(new string('y', 5000));
        sink.Write("after");

        Assert.Equal("after", File.ReadAllLines(sink.CurrentFile).Single());
        Assert.Equal(5000, File.ReadAllLines(sink.RotatedFile(1)).Single().Length);
    }

    [Fact]
    public void A_write_failure_is_recorded_and_never_thrown()
    {
        // The "directory" is a file, so nothing under it can be created.
        Directory.CreateDirectory(_directory);
        var blocker = Path.Combine(_directory, "logs");
        File.WriteAllText(blocker, "in the way");

        using var provider = new RollingFileLoggerProvider(new RollingFileLoggerOptions { Directory = blocker });
        var logger = provider.CreateLogger("x");

        logger.LogInformation("goes nowhere");

        Assert.NotNull(provider.Problem);
        Assert.Contains("Could not write", provider.Problem, StringComparison.Ordinal);
    }

    private static int LastLineNumber(string text)
    {
        var last = text.Split('\n', StringSplitOptions.RemoveEmptyEntries)[^1];
        return int.Parse(last.AsSpan(5, 3), System.Globalization.NumberStyles.None, System.Globalization.CultureInfo.InvariantCulture);
    }
}
