using Amazon;
using Amazon.Runtime;
using Amazon.SecretsManager;
using Amazon.SecretsManager.Model;
using Amazon.SecurityToken;
using Amazon.SecurityToken.Model;

namespace AiChatinator.Deploy;

public sealed record AwsIdentity(string Account, string Region);

public interface IAwsAccess
{
    Task<AwsIdentity> GetIdentityAsync(CancellationToken cancellationToken);

    Task PutSecretAsync(string secretArn, string value, CancellationToken cancellationToken);
}

/// <summary>
/// Uses the AWS SDK default credential/region chain. AWS_PROFILE and AWS_REGION are set by the
/// deployer before this runs, so --profile/--region behave like the AWS CLI flags.
/// </summary>
public sealed class AwsAccess : IAwsAccess
{
    public async Task<AwsIdentity> GetIdentityAsync(CancellationToken cancellationToken)
    {
        try
        {
            using var sts = new AmazonSecurityTokenServiceClient();
            var region = sts.Config.RegionEndpoint?.SystemName
                ?? throw new DeployException("No AWS region configured. Pass --region or set it in your AWS profile.");
            var identity = await sts.GetCallerIdentityAsync(new GetCallerIdentityRequest(), cancellationToken);
            return new AwsIdentity(identity.Account, region);
        }
        catch (AmazonServiceException ex)
        {
            throw new DeployException($"AWS rejected the credentials: {ex.Message}");
        }
        catch (AmazonClientException ex)
        {
            throw new DeployException($"AWS credentials/region are not usable: {ex.Message} " +
                "Configure them with 'aws configure' / 'aws sso login' or pass --profile and --region.");
        }
    }

    public async Task PutSecretAsync(string secretArn, string value, CancellationToken cancellationToken)
    {
        var region = Arn.Parse(secretArn).Region;
        using var secrets = new AmazonSecretsManagerClient(RegionEndpoint.GetBySystemName(region));
        await secrets.PutSecretValueAsync(new PutSecretValueRequest
        {
            SecretId = secretArn,
            SecretString = value
        }, cancellationToken);
    }
}
