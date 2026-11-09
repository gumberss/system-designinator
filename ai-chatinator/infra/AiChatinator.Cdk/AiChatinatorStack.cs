using Amazon.CDK;
using Amazon.CDK.AWS.Apigatewayv2;
using Amazon.CDK.AWS.Cognito;
using Amazon.CDK.AWS.DynamoDB;
using Amazon.CDK.AWS.Events;
using Amazon.CDK.AWS.Events.Targets;
using Amazon.CDK.AWS.IAM;
using Amazon.CDK.AWS.Lambda;
using Amazon.CDK.AWS.Lambda.EventSources;
using Amazon.CDK.AWS.Logs;
using Amazon.CDK.AWS.S3;
using Amazon.CDK.AWS.SecretsManager;
using Amazon.CDK.AWS.SNS;
using Amazon.CDK.AWS.SNS.Subscriptions;
using Amazon.CDK.AWS.SQS;
using Amazon.CDK.AwsApigatewayv2Integrations;
using Constructs;
using DynamoAttribute = Amazon.CDK.AWS.DynamoDB.Attribute;
using LambdaFn = Amazon.CDK.AWS.Lambda.Function;
using LambdaFunctionProps = Amazon.CDK.AWS.Lambda.FunctionProps;

public sealed class AiChatinatorStackProps : StackProps
{
    /// <summary>"dev" destroys every resource with the stack; "prod" retains stateful resources.</summary>
    public string Stage { get; init; } = "dev";

    /// <summary>Folder with the published AiChatinator.Api Lambda bundle (dotnet publish output).</summary>
    public string ApiAssetPath { get; init; } = Path.Combine("..", "..", "artifacts", "lambda", "api");

    /// <summary>Folder with the published AiChatinator.Worker Lambda bundle (dotnet publish output).</summary>
    public string WorkerAssetPath { get; init; } = Path.Combine("..", "..", "artifacts", "lambda", "worker");

    /// <summary>Ai:Provider for the API Lambda: LocalDemo or OpenAiCompatible.</summary>
    public string AiProvider { get; init; } = "LocalDemo";
}

public sealed class AiChatinatorStack : Stack
{
    public AiChatinatorStack(Construct scope, string id, AiChatinatorStackProps? props = null)
        : base(scope, id, props ??= new AiChatinatorStackProps())
    {
        var stage = props.Stage;
        if (stage is not ("dev" or "prod"))
        {
            throw new ArgumentException("Stage must be dev or prod.", nameof(props));
        }

        if (props.AiProvider is not ("LocalDemo" or "OpenAiCompatible"))
        {
            throw new ArgumentException("AiProvider must be LocalDemo or OpenAiCompatible.", nameof(props));
        }

        var isProd = stage == "prod";
        var removal = isProd ? RemovalPolicy.RETAIN : RemovalPolicy.DESTROY;
        Amazon.CDK.Tags.Of(this).Add("app", "ai-chatinator");
        Amazon.CDK.Tags.Of(this).Add("stage", stage);

        Table CreateTable(string id, string name, string? ttlAttribute) => new(this, id, new TableProps
        {
            TableName = name,
            TimeToLiveAttribute = ttlAttribute,
            PartitionKey = new DynamoAttribute { Name = "pk", Type = AttributeType.STRING },
            SortKey = new DynamoAttribute { Name = "sk", Type = AttributeType.STRING },
            BillingMode = BillingMode.PAY_PER_REQUEST,
            Encryption = TableEncryption.AWS_MANAGED,
            PointInTimeRecoverySpecification = new PointInTimeRecoverySpecification
            {
                PointInTimeRecoveryEnabled = isProd
            },
            RemovalPolicy = removal
        });

        var conversations = CreateTable("Conversations", "ai-chatinator-conversations", null);
        var usage = CreateTable("Usage", "ai-chatinator-usage", "expiresAt");
        var audit = CreateTable("Audit", "ai-chatinator-audit", "expiresAt");

        // gsi1: pending outbox events (conversations) and usage history by date (usage). Sparse by design.
        foreach (var table in new[] { conversations, usage })
        {
            table.AddGlobalSecondaryIndex(new GlobalSecondaryIndexProps
            {
                IndexName = "gsi1",
                PartitionKey = new DynamoAttribute { Name = "gsi1pk", Type = AttributeType.STRING },
                SortKey = new DynamoAttribute { Name = "gsi1sk", Type = AttributeType.STRING },
                ProjectionType = ProjectionType.ALL
            });
        }

        var files = new Bucket(this, "Files", new BucketProps
        {
            BlockPublicAccess = BlockPublicAccess.BLOCK_ALL,
            Encryption = BucketEncryption.S3_MANAGED,
            EnforceSSL = true,
            Versioned = true,
            RemovalPolicy = removal,
            AutoDeleteObjects = !isProd
        });

        var userPool = new UserPool(this, "UserPool", new UserPoolProps
        {
            UserPoolName = $"ai-chatinator-users-{stage}",
            SelfSignUpEnabled = true,
            SignInAliases = new SignInAliases { Email = true },
            AutoVerify = new AutoVerifiedAttrs { Email = true },
            StandardAttributes = new StandardAttributes
            {
                Email = new StandardAttribute { Required = true, Mutable = true }
            },
            PasswordPolicy = new PasswordPolicy
            {
                MinLength = 12,
                RequireDigits = true,
                RequireLowercase = true,
                RequireUppercase = true,
                RequireSymbols = true
            },
            RemovalPolicy = removal
        });

        var userPoolClient = userPool.AddClient("WebClient", new UserPoolClientOptions
        {
            UserPoolClientName = "ai-chatinator-web",
            GenerateSecret = false,
            AuthFlows = new AuthFlow { UserSrp = true },
            PreventUserExistenceErrors = true,
            EnableTokenRevocation = true
        });

        var domainEvents = new Topic(this, "DomainEvents", new TopicProps
        {
            TopicName = "ai-chatinator-domain-events",
            MasterKey = new Amazon.CDK.AWS.KMS.Key(this, "EventsKey", new Amazon.CDK.AWS.KMS.KeyProps
            {
                EnableKeyRotation = true,
                RemovalPolicy = removal,
                // KMS keys cannot be deleted immediately; 7 days is the shortest allowed waiting period.
                PendingWindow = isProd ? null : Duration.Days(7)
            })
        });

        var deadLetterQueue = new Queue(this, "WorkerDeadLetterQueue", new QueueProps
        {
            QueueName = "ai-chatinator-worker-dlq",
            Encryption = QueueEncryption.SQS_MANAGED,
            RetentionPeriod = Duration.Days(14),
            RemovalPolicy = removal
        });

        var workerQueue = new Queue(this, "WorkerQueue", new QueueProps
        {
            QueueName = "ai-chatinator-worker",
            Encryption = QueueEncryption.SQS_MANAGED,
            VisibilityTimeout = Duration.Minutes(2),
            RetentionPeriod = Duration.Days(4),
            DeadLetterQueue = new DeadLetterQueue
            {
                Queue = deadLetterQueue,
                MaxReceiveCount = 5
            },
            RemovalPolicy = removal
        });
        domainEvents.AddSubscription(new SqsSubscription(workerQueue));

        // No fixed name: a deleted secret keeps its name reserved during the recovery window.
        var openAiSecret = new Secret(this, "OpenAiApiKey", new SecretProps
        {
            Description = "OpenAI-compatible API key for ai-chatinator. Set with: aws secretsmanager put-secret-value",
            RemovalPolicy = removal
        });

        // Least-privilege runtime identity for the API and the worker (Lambda or ECS task).
        var appRole = new Role(this, "AppRuntimeRole", new RoleProps
        {
            AssumedBy = new CompositePrincipal(
                new ServicePrincipal("lambda.amazonaws.com"),
                new ServicePrincipal("ecs-tasks.amazonaws.com")),
            ManagedPolicies =
            [
                ManagedPolicy.FromAwsManagedPolicyName("service-role/AWSLambdaBasicExecutionRole")
            ]
        });
        conversations.GrantReadWriteData(appRole);
        usage.GrantReadWriteData(appRole);
        audit.GrantWriteData(appRole);
        files.GrantReadWrite(appRole);
        domainEvents.GrantPublish(appRole);
        workerQueue.GrantConsumeMessages(appRole);
        openAiSecret.GrantRead(appRole);

        var apiCode = AssetCode(props.ApiAssetPath, "API");
        var workerCode = AssetCode(props.WorkerAssetPath, "worker");

        LambdaFn CreateFunction(
            string id,
            Code code,
            string handler,
            Duration timeout,
            int memorySize,
            Dictionary<string, string> environment)
        {
            var logGroup = new LogGroup(this, $"{id}Logs", new Amazon.CDK.AWS.Logs.LogGroupProps
            {
                Retention = isProd ? RetentionDays.THREE_MONTHS : RetentionDays.TWO_WEEKS,
                RemovalPolicy = removal
            });
            return new LambdaFn(this, id, new LambdaFunctionProps
            {
                Runtime = Runtime.DOTNET_10,
                Architecture = Architecture.X86_64,
                Code = code,
                Handler = handler,
                Role = appRole,
                Timeout = timeout,
                MemorySize = memorySize,
                LogGroup = logGroup,
                Environment = environment
            });
        }

        var apiFunction = CreateFunction(
            "ApiFunction",
            apiCode,
            "AiChatinator.Api",
            Duration.Seconds(30),
            1024,
            new Dictionary<string, string>
            {
                ["ASPNETCORE_ENVIRONMENT"] = isProd ? "Production" : "Development",
                ["Persistence__Provider"] = "DynamoDb",
                ["Persistence__DynamoDb__ConversationsTable"] = conversations.TableName,
                ["Persistence__DynamoDb__UsageTable"] = usage.TableName,
                ["Persistence__DynamoDb__AuditTable"] = audit.TableName,
                ["DomainEvents__Transport"] = "Sns",
                ["DomainEvents__TopicArn"] = domainEvents.TopicArn,
                ["Auth__Mode"] = "CognitoJwt",
                ["Auth__Cognito__Authority"] = userPool.UserPoolProviderUrl,
                ["Auth__Cognito__ClientId"] = userPoolClient.UserPoolClientId,
                // Background loops are replaced by the worker and relay functions below.
                ["Outbox__DispatcherEnabled"] = "false",
                ["Worker__Enabled"] = "false",
                ["Mcp__ToolTransport"] = "InProcess",
                ["Ai__Provider"] = props.AiProvider,
                ["Ai__OpenAiCompatible__ApiKeySecretArn"] = openAiSecret.SecretArn
            });

        var workerEnvironment = new Dictionary<string, string>
        {
            ["CONVERSATIONS_TABLE"] = conversations.TableName,
            ["USAGE_TABLE"] = usage.TableName,
            ["AUDIT_TABLE"] = audit.TableName
        };

        // Must stay below the queue visibility timeout so a running batch is never redelivered.
        var workerFunction = CreateFunction(
            "WorkerFunction",
            workerCode,
            "AiChatinator.Worker::AiChatinator.Worker.SqsHandler::HandleAsync",
            Duration.Seconds(90),
            512,
            workerEnvironment);
        workerFunction.AddEventSource(new SqsEventSource(workerQueue, new SqsEventSourceProps
        {
            BatchSize = 10,
            ReportBatchItemFailures = true
        }));

        var relayFunction = CreateFunction(
            "OutboxRelayFunction",
            workerCode,
            "AiChatinator.Worker::AiChatinator.Worker.OutboxRelayHandler::HandleAsync",
            Duration.Seconds(50),
            512,
            new Dictionary<string, string>(workerEnvironment)
            {
                ["DOMAIN_EVENTS_TOPIC_ARN"] = domainEvents.TopicArn
            });
        new Rule(this, "OutboxRelaySchedule", new RuleProps
        {
            Description = "Publishes pending outbox events to SNS.",
            Schedule = Schedule.Rate(Duration.Minutes(1)),
            Targets = [new LambdaFunction(relayFunction)]
        });

        var httpApi = new HttpApi(this, "HttpApi", new HttpApiProps
        {
            ApiName = $"ai-chatinator-{stage}",
            DefaultIntegration = new HttpLambdaIntegration("ApiIntegration", apiFunction)
        });

        new CfnOutput(this, "Stage", new CfnOutputProps { Value = stage });
        new CfnOutput(this, "ApiUrl", new CfnOutputProps { Value = httpApi.ApiEndpoint });
        new CfnOutput(this, "OpenAiSecretArn", new CfnOutputProps { Value = openAiSecret.SecretArn });
        new CfnOutput(this, "FilesBucketName", new CfnOutputProps { Value = files.BucketName });
        new CfnOutput(this, "UserPoolId", new CfnOutputProps { Value = userPool.UserPoolId });
        new CfnOutput(this, "UserPoolClientId", new CfnOutputProps { Value = userPoolClient.UserPoolClientId });
        new CfnOutput(this, "DomainEventsTopicArn", new CfnOutputProps { Value = domainEvents.TopicArn });
        new CfnOutput(this, "WorkerQueueUrl", new CfnOutputProps { Value = workerQueue.QueueUrl });
        new CfnOutput(this, "ConversationsTableName", new CfnOutputProps { Value = conversations.TableName });
        new CfnOutput(this, "UsageTableName", new CfnOutputProps { Value = usage.TableName });
        new CfnOutput(this, "AuditTableName", new CfnOutputProps { Value = audit.TableName });
        new CfnOutput(this, "AppRuntimeRoleArn", new CfnOutputProps { Value = appRole.RoleArn });
    }

    private static Code AssetCode(string path, string label)
    {
        var fullPath = Path.GetFullPath(path);
        if (!Directory.Exists(fullPath))
        {
            throw new DirectoryNotFoundException(
                $"The {label} Lambda bundle was not found at '{fullPath}'. Run scripts/deploy.ps1 (it publishes the bundles) " +
                "or pass -c apiAsset=<dir> -c workerAsset=<dir>.");
        }

        return Code.FromAsset(fullPath);
    }
}
