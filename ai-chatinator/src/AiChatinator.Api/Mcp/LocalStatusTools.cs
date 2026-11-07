using System.ComponentModel;
using ModelContextProtocol.Server;

namespace AiChatinator.Api.Mcp;

[McpServerToolType]
public sealed class LocalStatusTools
{
    [McpServerTool]
    [Description("Returns the non-sensitive name and environment mode of this ai-chatinator instance.")]
    public static string GetLocalStatus() =>
        "ai-chatinator is running. This tool provides status only; it does not call an AI model or AWS.";
}
