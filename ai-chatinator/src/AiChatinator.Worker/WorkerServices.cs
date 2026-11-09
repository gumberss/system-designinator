using AiChatinator.Application;
using AiChatinator.Infrastructure;
using AiChatinator.Infrastructure.DynamoDb;
using AiChatinator.Infrastructure.Messaging;
using Amazon.DynamoDBv2;
using Amazon.SimpleNotificationService;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging;

namespace AiChatinator.Worker;

/// <summary>
/// Composition root shared by the worker Lambda handlers. Configuration comes from the
/// environment variables set by the CDK stack.
/// </summary>
public static class WorkerServices
{
    public static IServiceProvider Build(Func<string, string?>? readSetting = null)
    {
        readSetting ??= Environment.GetEnvironmentVariable;
        string Required(string name) => readSetting(name) is { Length: > 0 } value
            ? value
            : throw new InvalidOperationException($"Environment variable {name} is required.");

        var tableOptions = new DynamoDbTableOptions(
            Required("CONVERSATIONS_TABLE"),
            Required("USAGE_TABLE"),
            Required("AUDIT_TABLE"),
            int.TryParse(readSetting("AUDIT_RETENTION_DAYS"), out var days) ? days : 365);
        tableOptions.Validate();
        var dynamoServiceUrl = readSetting("DYNAMODB_SERVICE_URL");

        var services = new ServiceCollection();
        services.AddLogging(logging => logging.AddSimpleConsole(options => options.SingleLine = true));
        services.AddSingleton<IAmazonDynamoDB>(_ => string.IsNullOrWhiteSpace(dynamoServiceUrl)
            ? new AmazonDynamoDBClient()
            : new AmazonDynamoDBClient(new AmazonDynamoDBConfig { ServiceURL = dynamoServiceUrl }));
        services.AddSingleton(tableOptions);
        services.AddSingleton<DynamoDbChatStore>();
        services.AddSingleton<IOutboxStore>(sp => sp.GetRequiredService<DynamoDbChatStore>());
        services.AddSingleton<IUsageRollupStore>(sp => sp.GetRequiredService<DynamoDbChatStore>());
        services.AddSingleton<IDomainEventHandler, UsageRollupHandler>();
        services.AddSingleton<DomainEventMessageProcessor>();

        var topicArn = readSetting("DOMAIN_EVENTS_TOPIC_ARN");
        if (!string.IsNullOrWhiteSpace(topicArn))
        {
            services.AddSingleton<IAmazonSimpleNotificationService, AmazonSimpleNotificationServiceClient>();
            services.AddSingleton<IDomainEventPublisher>(sp => new SnsDomainEventPublisher(
                sp.GetRequiredService<IAmazonSimpleNotificationService>(),
                topicArn));
            services.AddSingleton<OutboxRelay>();
        }

        return services.BuildServiceProvider();
    }
}
