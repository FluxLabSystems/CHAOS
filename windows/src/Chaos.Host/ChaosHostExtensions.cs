using Chaos.Api;
using Chaos.Host.Abstractions;
using Chaos.Host.Supervisor;
using Chaos.Host.Configuration;
using Chaos.Host.Docs;
using Chaos.Host.Endpoints;
using Chaos.Host.Health;
using Chaos.Host.Proxy;
using Chaos.Host.Routing;
using Chaos.Host.Setup;
using Chaos.Host.Web;
using Microsoft.AspNetCore.Builder;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.DependencyInjection.Extensions;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Options;

namespace Chaos.Host;

/// <summary>
/// Composition root for the CHAOS gateway.
/// </summary>
/// <remarks>
/// Split from <c>Program.cs</c> so the whole host can be assembled from a test,
/// from the WinUI shell's in-process host, or from any other entry point,
/// without duplicating registration order — which matters here, because the
/// order hosted services run in is part of the design.
/// </remarks>
public static class ChaosHostExtensions
{
    /// <summary>
    /// The supervisor's section, relative to the gateway's <c>Chaos</c> section:
    /// <c>Chaos:Backend</c> in a file, <c>CHAOS_Backend__*</c> as a variable.
    /// </summary>
    public const string BackendSectionName = "Backend";

    /// <summary>
    /// Registers everything the gateway needs and applies
    /// <see cref="ChaosHostOptions.ListenUrl"/> to the web host.
    /// </summary>
    /// <param name="builder">The application builder.</param>
    /// <returns>The builder, for chaining.</returns>
    /// <exception cref="RouteOwnershipException">The route-ownership manifest is malformed or ambiguous.</exception>
    public static WebApplicationBuilder AddChaosHost(this WebApplicationBuilder builder)
    {
        ArgumentNullException.ThrowIfNull(builder);

        // The installed layout first, from a bootstrap read of the Chaos
        // section and the CHAOS_ variables: the config directory it names holds
        // chaos.host.json, which is itself a configuration source and has to
        // be in place before anything binds. On a checkout there is no layout
        // and this is a no-op that /host/info explains.
        var bootstrap = InstalledConfiguration.BuildChaosConfiguration(builder.Configuration);
        var layout = InstalledLayoutResolver.Resolve(
            bootstrap[nameof(ChaosHostOptions.ConfigDirectory)],
            bootstrap[nameof(ChaosHostOptions.LogDirectory)],
            InstalledLayoutResolver.PlatformRegistry(),
            InstalledLayoutResolver.PlatformProgramData());
        var hostConfigurationLoaded = InstalledConfiguration.AddHostConfigurationFile(builder.Configuration, layout);
        var installed = InstalledConfiguration.ReadBackendEnvironment(layout, hostConfigurationLoaded, out var backendEnvironment);
        builder.Services.AddSingleton(layout);
        builder.Services.AddSingleton(installed);

        // appsettings' "Chaos" section (now including the installed file),
        // overridden by CHAOS_-prefixed environment variables at the same
        // level: CHAOS_BackendUrl overrides Chaos:BackendUrl,
        // CHAOS_Routes__0__Owner overrides Chaos:Routes:0:Owner.
        var chaosConfiguration = InstalledConfiguration.BuildChaosConfiguration(builder.Configuration);

        // The table has to exist before the pipeline is built, so it is bound
        // eagerly here rather than resolved from IOptions later.
        var options = new ChaosHostOptions();
        chaosConfiguration.Bind(options);

        // One answer for "which database, which design package", handed to
        // setup, the supervisor and the ported endpoints alike. The supervisor
        // section is bound from the gateway's view so that CHAOS_Backend__X -
        // the form the desktop shell sets on a child it starts - reaches it.
        var backendSection = chaosConfiguration.GetSection(BackendSectionName);
        var storage = PlatformStorage.Resolve(
            options.DatabaseUrl,
            options.DataDirectory,
            backendSection[nameof(BackendSupervisorOptions.DatabaseUrl)],
            backendSection[nameof(BackendSupervisorOptions.DataDirectory)],
            backendEnvironment,
            layout);
        options.DatabaseUrl = storage.DatabaseUrl;
        options.DataDirectory = storage.DataDirectory;
        options.ConfigDirectory ??= layout.ConfigDirectory;
        options.LogDirectory ??= layout.LogDirectory;
        builder.Services.AddSingleton(storage);

        builder.Services.AddSingleton<IValidateOptions<ChaosHostOptions>, ChaosHostOptionsValidator>();
        builder.Services
            .AddOptions<ChaosHostOptions>()
            .Bind(chaosConfiguration)
            .PostConfigure(bound =>
            {
                bound.DatabaseUrl = storage.DatabaseUrl;
                bound.DataDirectory = storage.DataDirectory;
                bound.ConfigDirectory ??= layout.ConfigDirectory;
                bound.LogDirectory ??= layout.LogDirectory;
            })
            .ValidateOnStart();

        var table = RouteOwnershipTable.Build(
            RouteOwnershipDefaults.Manifest,
            [.. options.Routes],
            options.ReplaceDefaultRoutes);

        builder.Services.AddSingleton(table);
        builder.Services.AddSingleton<NativeRouteInventory>();
        builder.Services.AddSingleton<BackendHealthState>();
        builder.Services.AddSingleton<BackendForwarder>();

        var webRoot = WebRootResolver.Resolve(options, builder.Environment.ContentRootPath);
        builder.Services.AddSingleton(webRoot);

        // The documentation site is resolved here, not when the listener starts,
        // so /host/info can report where the gateway looked even in a process
        // where the listener never runs.
        var documentationRoot = DocumentationRootResolver.Resolve(
            options.Docs,
            webRoot,
            builder.Environment.ContentRootPath);
        builder.Services.AddSingleton(documentationRoot);
        builder.Services.AddSingleton(new DocumentationSiteState(options.Docs, documentationRoot));

        builder.Services.AddHttpForwarder();
        builder.Services
            .AddHttpClient(BackendHealthMonitor.HttpClientName)
            .ConfigureHttpClient(client =>
            {
                // The per-poll budget is enforced by a CancellationToken so a
                // timeout is distinguishable from a transport failure.
                client.Timeout = Timeout.InfiniteTimeSpan;
            });

        builder.Services.Configure<HostOptions>(host =>
        {
            host.ShutdownTimeout = options.ShutdownTimeout;

            // A background service that throws must not silently take the whole
            // host with it; the gateway's job during a failure is to stay up and
            // report it.
            host.BackgroundServiceExceptionBehavior = BackgroundServiceExceptionBehavior.Ignore;
        });

        // Order is deliberate:
        //  1. validate the manifest - refuse to start on a route that would 404;
        //  2. start first-run setup, so a fresh machine is being prepared while
        //     the backend is starting rather than after it has already failed
        //     against an empty database. It does not block startup - see
        //     SetupHostedService - so the shell can poll /host/setup and watch;
        //  3. start the backend (if a supervisor is registered);
        //  4. start polling the backend.
        //  5. start the documentation listener last. It is a second, read-only
        //     listener on its own port and nothing else in this process depends
        //     on it, so it goes at the back of the queue and cannot delay
        //     anything that matters more.
        builder.Services.AddHostedService<RouteOwnershipStartupCheck>();
        builder.Services.AddPlatformSetup();

        // The ported endpoints are always registered, even while the manifest
        // still routes their paths to Python. An endpoint that exists but is not
        // owned is inert; a route marked Dotnet with no endpoint behind it 404s,
        // which is why the startup check refuses that combination. Registering
        // first means flipping ownership is a configuration change.
        builder.Services.AddChaosApi(api => api.SqliteDatabasePath = storage.DatabaseUrl);

        // The real supervisor must be registered BEFORE the TryAdd below, which
        // is a fallback and not a default: TryAdd keeps whatever is already
        // there. Without this call the gateway would register
        // NullBackendSupervisor, start nothing, and proxy /api/v1 to a port
        // with no listener -- a console that loads and then answers nothing.
        //
        // Both registrations name the same IBackendSupervisor, from
        // Chaos.Host.Abstractions. That is what makes the TryAdd a fallback:
        // when the supervisor compiled its own mirror of the contract the two
        // interfaces merely shared a name, the TryAdd always won, and the
        // gateway reported "no supervisor registered" with supervision on.
        if (options.SuperviseBackend)
        {
            builder.Services.AddChaosBackendSupervisor(
                backendSection,
                supervisor =>
                {
                    // The gateway owns where the backend listens, because it is
                    // the thing that proxies to it. Two places naming that
                    // address would eventually disagree.
                    if (Uri.TryCreate(options.BackendUrl, UriKind.Absolute, out var backend))
                    {
                        supervisor.BindAddress = backend.Host;
                        supervisor.Port = backend.Port;
                    }

                    // And it owns the storage answer, resolved once above from
                    // every place it can be named. homestead.env goes to the
                    // child verbatim; a key already set from configuration
                    // keeps its configured value.
                    supervisor.DatabaseUrl = storage.DatabaseUrl;
                    supervisor.DataDirectory = storage.DataDirectory;
                    foreach (var (key, value) in backendEnvironment)
                    {
                        supervisor.Environment.TryAdd(key, value);
                    }

                    // The gateway drives the lifecycle through
                    // BackendSupervisorHost, bounded by Chaos:BackendStartTimeout
                    // and placed after route validation and first-run setup in
                    // the hosted-service order. The supervisor's own hosted
                    // service would start it a second time, unbounded, from
                    // wherever this call happens to sit; so it is switched off
                    // and the one budget is handed down as the supervisor's
                    // startup gate.
                    supervisor.RegisterHostedService = false;
                    supervisor.StartupGate = options.BackendStartTimeout;
                });
        }

        builder.Services.TryAddSingleton<IBackendSupervisor, NullBackendSupervisor>();
        builder.Services.AddHostedService<Supervision.BackendSupervisorHost>();
        builder.Services.AddHostedService<BackendHealthMonitor>();
        builder.Services.AddHostedService<DocumentationSiteHost>();

        WindowsServiceIntegration.AddWindowsServiceIfAvailable(builder);

        if (!string.IsNullOrWhiteSpace(options.ListenUrl))
        {
            builder.WebHost.UseUrls(
                options.ListenUrl.Split(';', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries));
        }

        return builder;
    }

    /// <summary>
    /// Builds the request pipeline: ownership fork, operator console, gateway
    /// endpoints, then captures the endpoint inventory for startup validation.
    /// </summary>
    /// <param name="app">The application.</param>
    /// <returns>The application, for chaining.</returns>
    public static WebApplication UseChaosHost(this WebApplication app)
    {
        ArgumentNullException.ThrowIfNull(app);

        app.MapChaosApi();

        var inventory = app.Services.GetRequiredService<NativeRouteInventory>();
        var webRoot = app.Services.GetRequiredService<WebRootResolution>();

        // First real decision in the pipeline. Python-owned paths never reach
        // the static-file middleware or the endpoint pipeline.
        app.UseMiddleware<RouteOwnershipMiddleware>();

        app.UseOperatorConsole(webRoot);

        app.UseRouting();
        app.MapChaosHostEndpoints();

        // Everything that will ever be mapped has been mapped; freeze the list
        // the startup check validates the manifest against.
        inventory.Capture(app);

        return app;
    }
}
