using Chaos.Shell.Core;

namespace Chaos.Shell.Core.Tests;

/// <summary>
/// What the running shell does with a second launch, from the string the
/// Windows App SDK actually delivers: the second process's command line.
/// </summary>
public sealed class RedirectedActivationTests
{
    [Theory]
    [InlineData("--annunciator")]
    [InlineData(@"""C:\Program Files\Project CHAOS\Chaos.Shell.exe"" --annunciator")]
    [InlineData(@"C:\CHAOS\Chaos.Shell.exe --annunciator")]
    [InlineData("--acknowledge")]
    [InlineData("--ack")]
    [InlineData("/annunciator")]
    public void The_start_menu_annunciator_shortcut_opens_the_annunciator(string launchArguments)
    {
        var redirected = RedirectedActivation.Parse(launchArguments);

        Assert.Equal(RedirectedAction.ShowAnnunciator, redirected.Action);
        Assert.True(redirected.Payload.WantsAnnunciator);
        Assert.Empty(redirected.Notices);
    }

    [Theory]
    [InlineData(null)]
    [InlineData("")]
    [InlineData("   ")]
    [InlineData(@"""C:\Program Files\Project CHAOS\Chaos.Shell.exe""")]
    public void A_plain_second_launch_focuses_the_console(string? launchArguments)
    {
        var redirected = RedirectedActivation.Parse(launchArguments);

        Assert.Equal(RedirectedAction.FocusConsole, redirected.Action);
        Assert.Equal(ActivationPayload.FocusConsole, redirected.Payload);
        Assert.Empty(redirected.Notices);
    }

    [Fact]
    public void A_wall_display_launch_keeps_its_placement_flags()
    {
        var redirected = RedirectedActivation.Parse(@"Chaos.Shell.exe --annunciator --monitor ""Wall panel"" --fullscreen --always-on-top");

        Assert.Equal(RedirectedAction.ShowAnnunciator, redirected.Action);
        Assert.Equal("Wall panel", redirected.Payload.Monitor);
        Assert.True(redirected.Payload.FullScreen);
        Assert.True(redirected.Payload.AlwaysOnTop);
    }

    [Fact]
    public void The_old_json_payload_is_not_mistaken_for_a_switch_and_falls_back_safely()
    {
        var redirected = RedirectedActivation.Parse(new ActivationPayload { Target = ActivationTarget.Annunciator }.Serialize());

        // JSON was never the wire format WinUI delivers. Whatever the
        // tokenizer makes of it, the answer is the safe one and nothing throws.
        Assert.Equal(RedirectedAction.FocusConsole, redirected.Action);
        Assert.Equal(ActivationPayload.FocusConsole, redirected.Payload);
    }

    [Fact]
    public void A_different_gateway_on_a_second_launch_is_reported_not_applied()
    {
        var redirected = RedirectedActivation.Parse("--host http://other-node:8080 --annunciator");

        Assert.Equal(RedirectedAction.ShowAnnunciator, redirected.Action);
        Assert.Contains(RedirectedActivation.HostOverrideIgnoredNotice, redirected.Notices);
    }

    [Fact]
    public void An_unknown_switch_is_reported_and_the_launch_still_lands_somewhere()
    {
        var redirected = RedirectedActivation.Parse("--annunciator --nonsense");

        Assert.Equal(RedirectedAction.ShowAnnunciator, redirected.Action);
        Assert.Contains(redirected.Notices, notice => notice.Contains("--nonsense", StringComparison.Ordinal));
    }

    [Fact]
    public void A_first_launch_and_a_redirected_launch_agree()
    {
        // The same shortcut must behave identically whether or not the shell
        // was already running: the redirected path parses with the same
        // parser the entry point uses.
        var arguments = new[] { "--acknowledge", "--monitor", "2" };
        var first = ShellCommandLine.Parse(arguments).Activation;
        var redirected = RedirectedActivation.Parse(string.Join(' ', arguments)).Payload;

        Assert.Equal(first, redirected);
    }
}
