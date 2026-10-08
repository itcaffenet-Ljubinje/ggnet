using static GgnetAgent.Tests.Samples;

namespace GgnetAgent.Tests;

public class PlannerTests
{
    private static readonly AgentAction ConnectTarget = new AgentAction.Connect("192.168.0.10", 3260, Target, "D");

    [Fact]
    public void Connects_when_provisioned_and_nothing_is_connected()
    {
        var actions = Planner.Plan(Config(), new LocalState(ExpectedIqn, null), true);
        Assert.Equal([ConnectTarget], actions);
    }

    [Fact]
    public void Does_nothing_when_already_connected()
    {
        Assert.Empty(Planner.Plan(Config(), new LocalState(ExpectedIqn, Target, "D"), true));
    }

    [Fact]
    public void Uses_the_letter_the_server_sets_for_this_machine()
    {
        var actions = Planner.Plan(Config() with { DriveLetter = "G" }, new LocalState(ExpectedIqn, null), true, "D");
        Assert.Equal([new AgentAction.Connect("192.168.0.10", 3260, Target, "G")], actions);
    }

    [Fact]
    public void Falls_back_to_the_local_letter_when_the_server_sends_none()
    {
        var actions = Planner.Plan(Config(), new LocalState(ExpectedIqn, null), true, "F");
        Assert.Equal([new AgentAction.Connect("192.168.0.10", 3260, Target, "F")], actions);
    }

    [Fact]
    public void Moves_a_connected_disk_when_the_letter_changes()
    {
        var local = new LocalState(ExpectedIqn, Target, "D");
        var actions = Planner.Plan(Config() with { DriveLetter = "E" }, local, true);
        // No disconnect: the idempotent connect script only changes the letter.
        Assert.Equal([new AgentAction.Connect("192.168.0.10", 3260, Target, "E")], actions);
    }

    [Theory]
    [InlineData("C")]
    [InlineData("d")]
    [InlineData("D'; whoami; '")]
    public void Rejects_an_invalid_drive_letter_from_the_server(string letter)
    {
        var config = Config() with { DriveLetter = letter };
        Assert.Throws<ServerException>(() => Planner.Plan(config, new LocalState(ExpectedIqn, null), true));
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
            [new AgentAction.Disconnect(Target), new AgentAction.Connect("192.168.0.10", 3260, other, "D")],
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
