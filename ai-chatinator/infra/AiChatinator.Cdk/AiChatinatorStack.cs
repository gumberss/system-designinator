using Amazon.CDK;
using Amazon.CDK.AWS.Cognito;
using Amazon.CDK.AWS.DynamoDB;
using Amazon.CDK.AWS.S3;
using Amazon.CDK.AWS.SNS;
using Amazon.CDK.AWS.SNS.Subscriptions;
using Amazon.CDK.AWS.SQS;
using Constructs;
using DynamoAttribute = Amazon.CDK.AWS.DynamoDB.Attribute;

public sealed class AiChatinatorStack : Stack
{
    public AiChatinatorStack(Construct scope, string id, StackProps? props = null)
        : base(scope, id, props)
    {
        var conversations = new Table(this, "Conversations", new TableProps
        {
            TableName = "ai-chatinator-conversations",
            PartitionKey = new DynamoAttribute { Name = "pk", Type = AttributeType.STRING },
            SortKey = new DynamoAttribute { Name = "sk", Type = AttributeType.STRING },
            BillingMode = BillingMode.PAY_PER_REQUEST,
            Encryption = TableEncryption.AWS_MANAGED,
            PointInTimeRecoverySpecification = new PointInTimeRecoverySpecification
            {
                PointInTimeRecoveryEnabled = true
            },
            RemovalPolicy = RemovalPolicy.RETAIN
        });

        var usage = new Table(this, "Usage", new TableProps
        {
            TableName = "ai-chatinator-usage",
            PartitionKey = new DynamoAttribute { Name = "pk", Type = AttributeType.STRING },
            SortKey = new DynamoAttribute { Name = "sk", Type = AttributeType.STRING },
            BillingMode = BillingMode.PAY_PER_REQUEST,
            Encryption = TableEncryption.AWS_MANAGED,
            PointInTimeRecoverySpecification = new PointInTimeRecoverySpecification
            {
                PointInTimeRecoveryEnabled = true
            },
            RemovalPolicy = RemovalPolicy.RETAIN
        });

        var audit = new Table(this, "Audit", new TableProps
        {
            TableName = "ai-chatinator-audit",
            PartitionKey = new DynamoAttribute { Name = "pk", Type = AttributeType.STRING },
            SortKey = new DynamoAttribute { Name = "sk", Type = AttributeType.STRING },
            BillingMode = BillingMode.PAY_PER_REQUEST,
            Encryption = TableEncryption.AWS_MANAGED,
            PointInTimeRecoverySpecification = new PointInTimeRecoverySpecification
            {
                PointInTimeRecoveryEnabled = true
            },
            RemovalPolicy = RemovalPolicy.RETAIN
        });

        var files = new Bucket(this, "Files", new BucketProps
        {
            BlockPublicAccess = BlockPublicAccess.BLOCK_ALL,
            Encryption = BucketEncryption.S3_MANAGED,
            EnforceSSL = true,
            Versioned = true,
            RemovalPolicy = RemovalPolicy.RETAIN
        });

        var userPool = new UserPool(this, "UserPool", new UserPoolProps
        {
            UserPoolName = "ai-chatinator-users",
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
            RemovalPolicy = RemovalPolicy.RETAIN
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
                RemovalPolicy = RemovalPolicy.RETAIN
            })
        });

        var deadLetterQueue = new Queue(this, "WorkerDeadLetterQueue", new QueueProps
        {
            QueueName = "ai-chatinator-worker-dlq",
            Encryption = QueueEncryption.SQS_MANAGED,
            RetentionPeriod = Duration.Days(14),
            RemovalPolicy = RemovalPolicy.RETAIN
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
            RemovalPolicy = RemovalPolicy.RETAIN
        });
        domainEvents.AddSubscription(new SqsSubscription(workerQueue));

        _ = conversations;
        _ = usage;
        _ = audit;
        _ = files;
        new CfnOutput(this, "UserPoolId", new CfnOutputProps { Value = userPool.UserPoolId });
        new CfnOutput(this, "UserPoolClientId", new CfnOutputProps { Value = userPoolClient.UserPoolClientId });
        new CfnOutput(this, "DomainEventsTopicArn", new CfnOutputProps { Value = domainEvents.TopicArn });
        new CfnOutput(this, "WorkerQueueUrl", new CfnOutputProps { Value = workerQueue.QueueUrl });
    }
}
