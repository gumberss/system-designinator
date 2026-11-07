using AiChatinator.Domain;

namespace AiChatinator.Application;

public interface IConversationFileStore
{
    Task<ConversationFile> SaveConversationFileAsync(
        ConversationFile file,
        CancellationToken cancellationToken);

    Task<IReadOnlyList<ConversationFile>> GetConversationFilesAsync(
        string userId,
        string conversationId,
        IReadOnlyCollection<string> fileIds,
        CancellationToken cancellationToken);

    Task<IReadOnlyList<ConversationFile>> ListConversationFilesAsync(
        string userId,
        string conversationId,
        CancellationToken cancellationToken);
}

public interface IFileTextExtractor
{
    Task<string> ExtractTextAsync(
        string fileName,
        string contentType,
        Stream content,
        CancellationToken cancellationToken);
}
