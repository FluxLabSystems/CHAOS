using Chaos.Host.Supervisor.Configuration;
using Xunit;

namespace Chaos.Host.Supervisor.Tests;

/// <summary>The installer's <c>homestead.env</c>, as an operator would edit it.</summary>
public sealed class EnvironmentFileTests
{
    [Fact]
    public void Reads_the_shipped_shape_comments_blanks_and_all()
    {
        var lines = new[]
        {
            "# Project CHAOS - Python backend overrides",
            "",
            "#CHAOS_NODE_ROLE=primary",
            "CHAOS_DATABASE_URL=sqlite:///C:/ProgramData/Project CHAOS/db/homestead.db",
            "  CHAOS_MQTT_ENABLED = false  ",
            "export CHAOS_SITE_ID=\"site.site.primary.01\"",
            "CHAOS_TIMEZONE='America/Toronto'",
            "CHAOS_EMPTY=",
        };

        var values = EnvironmentFile.Parse(lines, out var problems);

        Assert.Empty(problems);
        Assert.Equal(5, values.Count);
        Assert.Equal("sqlite:///C:/ProgramData/Project CHAOS/db/homestead.db", values["CHAOS_DATABASE_URL"]);
        Assert.Equal("false", values["CHAOS_MQTT_ENABLED"]);
        Assert.Equal("site.site.primary.01", values["CHAOS_SITE_ID"]);
        Assert.Equal("America/Toronto", values["CHAOS_TIMEZONE"]);
        Assert.Equal(string.Empty, values["CHAOS_EMPTY"]);
        Assert.False(values.ContainsKey("CHAOS_NODE_ROLE"));
    }

    [Fact]
    public void A_bad_line_is_reported_with_its_number_and_the_rest_still_reads()
    {
        var values = EnvironmentFile.Parse(["CHAOS_A=1", "this is not a setting", "=nokey", "CHAOS_B=2", "BAD KEY=3"], out var problems);

        Assert.Equal(["CHAOS_A", "CHAOS_B"], values.Keys.Order(StringComparer.Ordinal));
        Assert.Equal(3, problems.Count);
        Assert.Contains("line 2", problems[0], StringComparison.Ordinal);
        Assert.Contains("line 3", problems[1], StringComparison.Ordinal);
        Assert.Contains("line 5", problems[2], StringComparison.Ordinal);
    }

    [Fact]
    public void The_last_definition_wins_and_values_are_taken_literally()
    {
        var values = EnvironmentFile.Parse(["K=first", "K=$HOME/second # not a comment"], out _);

        Assert.Equal("$HOME/second # not a comment", values["K"]);
    }

    [Fact]
    public void A_missing_file_is_one_problem_and_no_values()
    {
        var values = EnvironmentFile.Read(Path.Combine(Path.GetTempPath(), "chaos-no-such-" + Guid.NewGuid().ToString("N"), "homestead.env"), out var problems);

        Assert.Empty(values);
        Assert.Single(problems);
        Assert.Contains("does not exist", problems[0], StringComparison.Ordinal);
    }

    [Fact]
    public void No_path_means_nothing_to_read_and_nothing_wrong()
    {
        var values = EnvironmentFile.Read(null, out var problems);

        Assert.Empty(values);
        Assert.Empty(problems);
    }
}
