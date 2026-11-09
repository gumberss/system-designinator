using Amazon.CDK;
using Amazon.CDK.Assertions;

namespace AiChatinator.Tests;

public sealed class CdkStackTests : IDisposable
{
    private readonly string _assetRoot = Path.Combine(Path.GetTempPath(), $"cdk-assets-{Guid.NewGuid():N}");

    public CdkStackTests()
    {
        foreach (var name in new[] { "api", "worker" })
        {
            Directory.CreateDirectory(Path.Combine(_assetRoot, name));
            File.WriteAllText(Path.Combine(_assetRoot, name, "placeholder.txt"), name);
        }
    }

    public void Dispose()
    {
        try { Directory.Delete(_assetRoot, recursive: true); } catch (IOException) { }
    }

    private Template Synth(string stage = "dev", string aiProvider = "LocalDemo") =>
        Template.FromStack(new AiChatinatorStack(new App(), "TestStack", new AiChatinatorStackProps
        {
            Stage = stage,
            AiProvider = aiProvider,
            ApiAssetPath = Path.Combine(_assetRoot, "api"),
            WorkerAssetPath = Path.Combine(_assetRoot, "worker")
        }));

    [Fact]
    public void Stack_synthesizes_expected_foundation_resources()
    {
        var template = Synth();

        template.ResourceCountIs("AWS::DynamoDB::Table", 3);
        template.ResourceCountIs("AWS::S3::Bucket", 1);
        template.ResourceCountIs("AWS::Cognito::UserPool", 1);
        template.ResourceCountIs("AWS::Cognito::UserPoolClient", 1);
        template.ResourceCountIs("AWS::SNS::Topic", 1);
        template.ResourceCountIs("AWS::SQS::Queue", 2);
        template.ResourceCountIs("AWS::SNS::Subscription", 1);
        template.ResourceCountIs("AWS::SecretsManager::Secret", 1);
        template.ResourceCountIs("AWS::ApiGatewayV2::Api", 1);

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

    [Fact]
    public void Tables_match_the_dynamodb_adapter_key_design()
    {
        var template = Synth();

        foreach (var tableName in new[] { "ai-chatinator-conversations", "ai-chatinator-usage" })
        {
            template.HasResourceProperties("AWS::DynamoDB::Table", Match.ObjectLike(new Dictionary<string, object>
            {
                ["TableName"] = tableName,
                ["GlobalSecondaryIndexes"] = Match.ArrayWith([Match.ObjectLike(new Dictionary<string, object>
                {
                    ["IndexName"] = "gsi1"
                })])
            }));
        }

        foreach (var tableName in new[] { "ai-chatinator-usage", "ai-chatinator-audit" })
        {
            template.HasResourceProperties("AWS::DynamoDB::Table", Match.ObjectLike(new Dictionary<string, object>
            {
                ["TableName"] = tableName,
                ["TimeToLiveSpecification"] = new Dictionary<string, object>
                {
                    ["AttributeName"] = "expiresAt",
                    ["Enabled"] = true
                }
            }));
        }

        template.HasOutput("ConversationsTableName", Match.AnyValue());
        template.HasOutput("WorkerQueueUrl", Match.AnyValue());
        template.HasOutput("ApiUrl", Match.AnyValue());
        template.HasOutput("OpenAiSecretArn", Match.AnyValue());
    }

    [Fact]
    public void Dev_stage_deletes_everything_on_destroy()
    {
        var template = Synth("dev");

        template.AllResources("AWS::DynamoDB::Table", Match.ObjectLike(new Dictionary<string, object>
        {
            ["DeletionPolicy"] = "Delete"
        }));
        template.AllResources("AWS::S3::Bucket", Match.ObjectLike(new Dictionary<string, object>
        {
            ["DeletionPolicy"] = "Delete"
        }));
        template.AllResources("AWS::Cognito::UserPool", Match.ObjectLike(new Dictionary<string, object>
        {
            ["DeletionPolicy"] = "Delete"
        }));
        template.ResourceCountIs("Custom::S3AutoDeleteObjects", 1);
        template.HasResourceProperties("AWS::KMS::Key", Match.ObjectLike(new Dictionary<string, object>
        {
            ["PendingWindowInDays"] = 7
        }));
    }

    [Fact]
    public void Prod_stage_retains_data()
    {
        var template = Synth("prod");

        template.AllResources("AWS::DynamoDB::Table", Match.ObjectLike(new Dictionary<string, object>
        {
            ["DeletionPolicy"] = "Retain"
        }));
        template.AllResources("AWS::S3::Bucket", Match.ObjectLike(new Dictionary<string, object>
        {
            ["DeletionPolicy"] = "Retain"
        }));
        template.ResourceCountIs("Custom::S3AutoDeleteObjects", 0);
        template.ResourceCountIs("AWS::IAM::Role", 1);
        template.AllResourcesProperties("AWS::DynamoDB::Table", Match.ObjectLike(new Dictionary<string, object>
        {
            ["PointInTimeRecoverySpecification"] = new Dictionary<string, object>
            {
                ["PointInTimeRecoveryEnabled"] = true
            }
        }));
    }

    [Fact]
    public void Lambdas_are_wired_to_the_api_queue_and_schedule()
    {
        var template = Synth("prod", "OpenAiCompatible");

        template.ResourceCountIs("AWS::Lambda::Function", 3);
        template.AllResourcesProperties("AWS::Lambda::Function", Match.ObjectLike(new Dictionary<string, object>
        {
            ["Runtime"] = "dotnet10"
        }));
        template.HasResourceProperties("AWS::Lambda::Function", Match.ObjectLike(new Dictionary<string, object>
        {
            ["Handler"] = "AiChatinator.Api",
            ["Environment"] = Match.ObjectLike(new Dictionary<string, object>
            {
                ["Variables"] = Match.ObjectLike(new Dictionary<string, object>
                {
                    ["Ai__Provider"] = "OpenAiCompatible",
                    ["Auth__Mode"] = "CognitoJwt",
                    ["Mcp__ToolTransport"] = "InProcess",
                    ["Worker__Enabled"] = "false",
                    ["Outbox__DispatcherEnabled"] = "false"
                })
            })
        }));
        template.HasResourceProperties("AWS::Lambda::Function", Match.ObjectLike(new Dictionary<string, object>
        {
            ["Handler"] = "AiChatinator.Worker::AiChatinator.Worker.SqsHandler::HandleAsync"
        }));
        template.HasResourceProperties("AWS::Lambda::Function", Match.ObjectLike(new Dictionary<string, object>
        {
            ["Handler"] = "AiChatinator.Worker::AiChatinator.Worker.OutboxRelayHandler::HandleAsync"
        }));
        template.HasResourceProperties("AWS::Lambda::EventSourceMapping", Match.ObjectLike(new Dictionary<string, object>
        {
            ["FunctionResponseTypes"] = new[] { "ReportBatchItemFailures" },
            ["BatchSize"] = 10
        }));
        template.HasResourceProperties("AWS::Events::Rule", Match.ObjectLike(new Dictionary<string, object>
        {
            ["ScheduleExpression"] = "rate(1 minute)"
        }));
        template.ResourceCountIs("AWS::Logs::LogGroup", 3);
    }

    [Theory]
    [InlineData("staging", "LocalDemo")]
    [InlineData("dev", "Unknown")]
    public void Invalid_settings_are_rejected(string stage, string aiProvider)
    {
        Assert.ThrowsAny<Exception>(() => Synth(stage, aiProvider));
    }

    [Fact]
    public void Missing_lambda_package_explains_how_to_build_it()
    {
        var error = Assert.Throws<DirectoryNotFoundException>(() =>
            new AiChatinatorStack(new App(), "TestStack", new AiChatinatorStackProps
            {
                ApiAssetPath = Path.Combine(_assetRoot, "missing"),
                WorkerAssetPath = Path.Combine(_assetRoot, "worker")
            }));
        Assert.Contains("missing", error.Message);
    }
}
