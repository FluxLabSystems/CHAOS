namespace Chaos.Shell.Core;

/// <summary>What the running shell does with a launch that was redirected to it.</summary>
public enum RedirectedAction
{
    /// <summary>Restore and focus the operator console. The safe answer to anything unclear.</summary>
    FocusConsole = 0,

    /// <summary>Open, or bring forward, the annunciator window.</summary>
    ShowAnnunciator = 1,
}

/// <summary>
/// A second launch, as the running instance should act on it.
/// </summary>
/// <remarks>
/// <para>
/// Windows App SDK single-instancing delivers the second process's command
/// line to the first as the <em>launch arguments</em> of the redirected
/// activation — one string, possibly led by the executable path, never JSON.
/// The running instance used to parse its <em>own</em> command line as a JSON
/// payload instead, which always failed closed to the console: the Start-menu
/// "Annunciator" shortcut therefore opened the console whenever the shell was
/// already running, and <c>--acknowledge</c>, <c>--monitor</c> and
/// <c>--fullscreen</c> were dropped.
/// </para>
/// <para>
/// This type is the whole decision, kept out of <c>App.xaml.cs</c> so it is
/// tested on every platform: split the string the way <c>CommandLineToArgvW</c>
/// would, parse it exactly as a first launch is parsed, and reduce the result
/// to one of two actions plus the notices the operator should see.
/// </para>
/// </remarks>
public sealed record RedirectedActivation(
    RedirectedAction Action,
    ActivationPayload Payload,
    IReadOnlyList<string> Notices)
{
    /// <summary>The notice shown when a redirected launch carried <c>--host</c>.</summary>
    public const string HostOverrideIgnoredNotice =
        "A second launch asked for a different gateway address with --host. The running shell keeps "
        + "the address it started with; exit it first to start against another gateway.";

    /// <summary>
    /// Decides what to do with a redirected launch.
    /// </summary>
    /// <param name="launchArguments">
    /// The redirected activation's launch arguments — the second launch's
    /// command line, with or without a leading executable path. Null, empty
    /// or unreadable means "focus the console".
    /// </param>
    /// <returns>The action, the payload it was derived from, and anything to tell the operator.</returns>
    public static RedirectedActivation Parse(string? launchArguments)
    {
        var arguments = CommandLineTokenizer.SplitActivationArguments(launchArguments);
        var options = ShellCommandLine.Parse(arguments);

        var notices = new List<string>(options.Problems);
        if (!string.IsNullOrWhiteSpace(options.HostOverride))
        {
            notices.Add(HostOverrideIgnoredNotice);
        }

        var action = options.Activation.WantsAnnunciator
            ? RedirectedAction.ShowAnnunciator
            : RedirectedAction.FocusConsole;

        return new RedirectedActivation(action, options.Activation, notices);
    }
}
