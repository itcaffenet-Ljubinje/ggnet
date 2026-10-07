using static GgnetAgent.Tests.Samples;

namespace GgnetAgent.Tests;

public class PlannerTests
{
    private static readonly AgentAction ConnectTarget = new AgentAction.Connect("192.168.0.10", 3260, Target);

    [Fact]
    public void Connects_when_provisioned_and_nothing_is_connected()
    {
        var actions = Planner.Plan(Config(), new LocalState(ExpectedIqn, null), true);
        Assert.Equal([ConnectTarget], actions);
    }

    [Fact]
    public void Does_nothing_when_already_connected()
    {
        Assert.Empty(Planner.Plan(Config(), new LocalState(ExpectedIqn, Target), true));
    }

    [Fact]
    public void Keeps_the_connection_when_the_server_is_unreachable()
    {
        Assert.Empty(Planner.Plan(null, new LocalState(ExpectedIqn, Target), true));
    }

    [Theory]
    [InlineData("idle")]
    [InlineData("error")]
    public void Disconnects_when_the_server_wants_no_target(string status)
    {
        var actions = Planner.Plan(Config(null, status), new LocalState(ExpectedIqn, Target), true);
        Assert.Equal([new AgentAction.Disconnect(Target)], actions);
    }

    [Fact]
    public void Switches_to_a_new_target_by_disconnecting_first()
    {
        const string other = "iqn.2025-05.net.ggnet:client-pc02";
        var actions = Planner.Plan(Config(other), new LocalState(ExpectedIqn, Target), true);
        Assert.Equal(
            [new AgentAction.Disconnect(Target), new AgentAction.Connect("192.168.0.10", 3260, other)],
            actions);
    }

    [Fact]
    public void Renames_the_initiator_before_connecting()
    {
        const string domainIqn = "iqn.1991-05.com.microsoft:pc01.cafe.local";
        var actions = Planner.Plan(Config(), new LocalState(domainIqn, null), true);
        Assert.Equal([new AgentAction.SetInitiatorName(domainIqn, ExpectedIqn), ConnectTarget], actions);
    }

    [Fact]
    public void Leaves_the_initiator_name_alone_when_not_managed()
    {
        const string domainIqn = "iqn.1991-05.com.microsoft:pc01.cafe.local";
        Assert.Equal([ConnectTarget], Planner.Plan(Config(), new LocalState(domainIqn, null), false));
    }

    [Theory]
    [InlineData("iqn.2025-05.net.ggnet:client-pc01'; Remove-Item C:\\ -Recurse; '", "192.168.0.10", 3260)]
    [InlineData(Target, "0.0.0.0", 3260)]
    [InlineData(Target, "server.local", 3260)]
    [InlineData(Target, "192.168.0.10", 0)]
    public void Rejects_an_invalid_target_from_the_server(string target, string ip, int port)
    {
        var config = Config(target) with { PortalIp = ip, PortalPort = port };
        Assert.Throws<ServerException>(() => Planner.Plan(config, new LocalState(ExpectedIqn, null), true));
    }

    [Fact]
    public void Rejects_an_invalid_expected_initiator_name()
    {
        var config = Config() with { InitiatorIqn = "pc01'; whoami; '" };
        var local = new LocalState("iqn.1991-05.com.microsoft:pc01.cafe.local", null);
        Assert.Throws<ServerException>(() => Planner.Plan(config, local, true));
    }
}
