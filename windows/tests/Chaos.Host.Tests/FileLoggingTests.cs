using Chaos.Host.Configuration;
using Chaos.Host.Setup;
using Chaos.Host.Tests.Support;

namespace Chaos.Host.Tests;

/// <summary>On an installed node the gateway writes log files where the manuals say they are.</summary>
public sealed class FileLoggingTests
{
    [Fact]
    public async Task The_gateway_log_lands_in_the_installed_logs_folder()
    {
        using var temp = TempInstalledLayout.Create();
        using var factory = ChaosHostFactory.For()
            .With("ConfigDirectory", temp.ConfigDirectory)
            .Build();
        using var client = factory.CreateClient();

        var info = await (await client.GetAsync("/host/info")).ReadJsonAsync();
        var expected = Path.Combine(temp.Root, "logs", "chaos-host.log");
        Assert.Equal(expected, info.GetProperty("installation").GetStringProperty("hostLogFile"));

        // Startup has logged by the time a request is answered: route
        // ownership validation is the first hosted service.
        var text = File.ReadAllText(expected);
        Assert.Contains("Route ownership validated", text, StringComparison.Ordinal);
        Assert.Contains("[INF] Chaos.Host.Routing.RouteOwnershipStartupCheck", text, StringComparison.Ordinal);
    }

    [Fact]
    public async Task The_setup_transcript_defaults_to_the_same_folder()
    {
        using var temp = TempInstalledLayout.Create();
        using var factory = ChaosHostFactory.For()
            .With("ConfigDirectory", temp.ConfigDirectory)
            .Build();
        using var client = factory.CreateClient();

        var setup = await (await client.GetAsync("/host/setup")).ReadJsonAsync();
        Assert.Equal(
            Path.Combine(temp.Root, "logs", SetupLogFactory.FileName),
            setup.GetProperty("configuration").GetStringProperty("logPath"));
    }

    [Fact]
    public void A_checkout_with_no_log_directory_writes_no_files()
    {
        var options = new ChaosHostOptions();
        Assert.Null(options.LogDirectory);

        // The transcript keeps its per-user fallback rather than inventing a logs folder.
        var log = SetupLogFactory.Create(options);
        Assert.DoesNotContain(Path.DirectorySeparatorChar + "logs" + Path.DirectorySeparatorChar, log.Path ?? string.Empty, StringComparison.Ordinal);
    }
}
