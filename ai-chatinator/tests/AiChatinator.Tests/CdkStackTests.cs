using Amazon.CDK;
using Amazon.CDK.Assertions;

namespace AiChatinator.Tests;

public sealed class CdkStackTests
{
    [Fact]
    public void Stack_synthesizes_expected_foundation_resources()
    {
        var app = new App();
        var stack = new AiChatinatorStack(app, "TestStack");
        var template = Template.FromStack(stack);

        template.ResourceCountIs("AWS::DynamoDB::Table", 3);
        template.ResourceCountIs("AWS::S3::Bucket", 1);
        template.ResourceCountIs("AWS::Cognito::UserPool", 1);
        template.ResourceCountIs("AWS::Cognito::UserPoolClient", 1);
        template.ResourceCountIs("AWS::SNS::Topic", 1);
        template.ResourceCountIs("AWS::SQS::Queue", 2);
        template.ResourceCountIs("AWS::SNS::Subscription", 1);

        template.HasResourceProperties(
            "AWS::S3::Bucket",
            new Dictionary<string, object>
            {
                ["PublicAccessBlockConfiguration"] = new Dictionary<string, object>
                {
                    ["BlockPublicAcls"] = true,
                    ["BlockPublicPolicy"] = true,
                    ["IgnorePublicAcls"] = true,
                    ["RestrictPublicBuckets"] = true
                },
                ["VersioningConfiguration"] = new Dictionary<string, object>
                {
                    ["Status"] = "Enabled"
                }
            });
    }
}
