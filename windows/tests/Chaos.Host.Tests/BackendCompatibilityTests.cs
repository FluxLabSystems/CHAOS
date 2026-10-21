using System.Net;
using Chaos.Host.Tests.Support;

namespace Chaos.Host.Tests;

/// <summary>
/// A backend on the wrong platform version: always reported, refused only
/// when <c>Chaos:RefuseIncompatibleBackend</c> says so.
/// </summary>
public sealed class BackendCompatibilityTests
{
    [Fact]
    public async Task Refuses_to_proxy_when_told_to_and_names_both_versions()
    {
        await using var backend = await StubBackend.StartAsync();
        backend.Settings.HealthVersion = "9.9.0";

        using var factory = ChaosHostFactory.For(backend)
            .With("RefuseIncompatibleBackend", "true")
            .Build();
        using var client = factory.CreateClient();

        await WaitForMismatchAsync(client);

        using var response = await client.GetAsync("/api/v1/alarms/active");
        Assert.Equal(HttpStatusCode.BadGateway, response.StatusCode);

        var body = await response.ReadJsonAsync();
        Assert.Equal("backend_incompatible", body.GetStringProperty("error"));
        Assert.Equal("9.9.0", body.GetProperty("backend").GetStringProperty("reportedVersion"));
        Assert.Equal("0.4.0", body.GetProperty("backend").GetStringProperty("platformVersionContract"));
        Assert.Contains("9.9.0", body.GetStringProperty("detail"), StringComparison.Ordinal);
        Assert.Contains("0.4.0", body.GetStringProperty("detail"), StringComparison.Ordinal);
        Assert.Equal("/api/v1/alarms", body.GetProperty("route").GetStringProperty("prefix"));

        // The backend never saw the request.
        Assert.DoesNotContain(backend.Requests, request => request.Path == "/api/v1/alarms/active");

        // The gateway's own endpoints are unaffected: they are how the
        // mismatch is seen, and /health says degraded with the versions.
        using var health = await client.GetAsync("/health");
        Assert.Equal(HttpStatusCode.ServiceUnavailable, health.StatusCode);
        var healthBody = await health.ReadJsonAsync();
        Assert.Equal("degraded", healthBody.GetStringProperty("status"));
        Assert.False(healthBody.GetProperty("backendDetail").GetProperty("versionCompatible").GetBoolean());

        using var live = await client.GetAsync("/health/live");
        Assert.Equal(HttpStatusCode.OK, live.StatusCode);
    }

    [Fact]
    public async Task Proxies_anyway_by_default_while_still_reporting_the_mismatch()
    {
        await using var backend = await StubBackend.StartAsync();
        backend.Settings.HealthVersion = "9.9.0";

        using var factory = ChaosHostFactory.For(backend).Build();
        using var client = factory.CreateClient();

        await WaitForMismatchAsync(client);

        using var response = await client.GetAsync("/api/v1/alarms/active");
        Assert.Equal(HttpStatusCode.OK, response.StatusCode);
        Assert.Equal("stub-python-backend", (await response.ReadJsonAsync()).GetStringProperty("served_by"));

        var health = await (await client.GetAsync("/health")).ReadJsonAsync();
        Assert.Equal("degraded", health.GetStringProperty("status"));
        Assert.Equal("9.9.0", health.GetProperty("backendDetail").GetStringProperty("reportedVersion"));
    }

    [Fact]
    public async Task A_compatible_backend_is_proxied_with_the_option_on()
    {
        await using var backend = await StubBackend.StartAsync();

        using var factory = ChaosHostFactory.For(backend)
            .With("RefuseIncompatibleBackend", "true")
            .Build();
        using var client = factory.CreateClient();

        await TestExtensions.WaitUntilAsync(
            async () => (await (await client.GetAsync("/health")).ReadJsonAsync()).GetStringProperty("backend") == "up",
            "the stub backend to be reported up");

        using var response = await client.GetAsync("/api/v1/alarms/active");
        Assert.Equal(HttpStatusCode.OK, response.StatusCode);
    }

    private static Task WaitForMismatchAsync(HttpClient client) =>
        TestExtensions.WaitUntilAsync(
            async () =>
            {
                var body = await (await client.GetAsync("/health")).ReadJsonAsync();
                var compatible = body.GetProperty("backendDetail").GetProperty("versionCompatible");
                return compatible.ValueKind == System.Text.Json.JsonValueKind.False;
            },
            "the gateway to notice the backend's platform version");
}
