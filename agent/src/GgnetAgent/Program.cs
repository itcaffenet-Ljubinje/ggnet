using GgnetAgent;
using Microsoft.Extensions.Options;

var builder = Host.CreateApplicationBuilder(new HostApplicationBuilderSettings
{
    Args = args,
    // As a service the working directory is System32; read appsettings.json next to the exe.
    ContentRootPath = AppContext.BaseDirectory,
});

builder.Services.AddWindowsService(o => o.ServiceName = "ggnet-agent");

// Add new hardware: the server booted a clone of the image in a VM to install a PC's devices.
if (File.Exists(Path.Combine(ToolchainJob.Folder, ToolchainJob.JobFile)))
{
    builder.Services.AddSingleton<IDeviceInstaller, SetupApiInstaller>();
    builder.Services.AddSingleton<IPowerControl, WindowsPowerControl>();
    builder.Services.AddHostedService<ToolchainWorker>();
    builder.Build().Run();
    return;
}

builder.Services.AddOptions<AgentOptions>()
    .Bind(builder.Configuration.GetSection(AgentOptions.Section))
    .ValidateOnStart();
builder.Services.AddSingleton<IValidateOptions<AgentOptions>, AgentOptionsValidator>();

builder.Services.AddHttpClient<ServerClient>((sp, http) =>
{
    var options = sp.GetRequiredService<IOptions<AgentOptions>>().Value;
    http.BaseAddress = new Uri(options.ServerUrl.TrimEnd('/') + "/");
    http.Timeout = TimeSpan.FromSeconds(10);
});
builder.Services.AddSingleton<IScriptRunner, PowerShellRunner>();
builder.Services.AddSingleton<IIscsiInitiator, WindowsIscsiInitiator>();
builder.Services.AddSingleton<IInventory, WindowsInventory>();
builder.Services.AddSingleton<IPowerControl, WindowsPowerControl>();
builder.Services.AddSingleton<ILocalDisks, WindowsLocalDisks>();
builder.Services.AddSingleton<IIdleTasks, WindowsIdleTasks>();
builder.Services.AddSingleton<IUefiVariables, WindowsUefiVariables>();
builder.Services.AddSingleton(TimeProvider.System);
builder.Services.AddHostedService<AgentWorker>();

builder.Build().Run();

/// <summary>Reports every settings problem by name instead of one generic message.</summary>
internal sealed class AgentOptionsValidator : IValidateOptions<AgentOptions>
{
    public ValidateOptionsResult Validate(string? name, AgentOptions options) =>
        options.Validate() is { Count: > 0 } errors
            ? ValidateOptionsResult.Fail(errors)
            : ValidateOptionsResult.Success;
}
