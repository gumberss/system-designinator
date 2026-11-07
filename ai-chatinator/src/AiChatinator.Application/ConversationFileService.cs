using System.Security.Cryptography;
using System.Text;
using AiChatinator.Domain;

namespace AiChatinator.Application;

public sealed class ConversationFileService(
    IConversationFileStore store,
    IFileTextExtractor textExtractor,
    ConversationFileOptions options)
{
    private const int MaximumFileNameLength = 128;
    private const int MaximumExtractedCharacters = 40_000;

    public async Task<ConversationFile> UploadAsync(
        string userId,
        string conversationId,
        string fileName,
        string contentType,
        Stream content,
        CancellationToken cancellationToken)
    {
        var safeFileName = NormalizeFileName(fileName);
        var normalizedContentType = NormalizeContentType(safeFileName, contentType);
        var bytes = await ReadBoundedAsync(content, options.MaximumBytes, cancellationToken);
        ValidateSignature(normalizedContentType, bytes);
        await using var fileStream = new MemoryStream(bytes, writable: false);
        var extractedText = await textExtractor.ExtractTextAsync(
            safeFileName,
            normalizedContentType,
            fileStream,
            cancellationToken);
        if (string.IsNullOrWhiteSpace(extractedText))
        {
            throw new InvalidDataException("The file contains no extractable text.");
        }

        if (extractedText.Length > MaximumExtractedCharacters)
        {
            extractedText = extractedText[..MaximumExtractedCharacters];
        }

        var file = new ConversationFile(
            Guid.NewGuid().ToString("N"),
            userId,
            conversationId,
            safeFileName,
            normalizedContentType,
            bytes.LongLength,
            Convert.ToHexString(SHA256.HashData(bytes)),
            extractedText,
            DateTimeOffset.UtcNow);
        return await store.SaveConversationFileAsync(file, cancellationToken);
    }

    private static string NormalizeFileName(string fileName)
    {
        var leafName = fileName.Replace('\\', '/').Split('/').LastOrDefault()?.Trim();
        if (string.IsNullOrWhiteSpace(leafName)
            || leafName.Length > MaximumFileNameLength
            || leafName.Any(char.IsControl))
        {
            throw new InvalidDataException("The file name is invalid or too long.");
        }

        return leafName;
    }

    private static string NormalizeContentType(string fileName, string contentType)
    {
        var extension = Path.GetExtension(fileName).ToLowerInvariant();
        return (extension, contentType.Split(';', 2)[0].Trim().ToLowerInvariant()) switch
        {
            (".txt", "text/plain") => "text/plain",
            (".md", "text/markdown" or "text/plain") => "text/markdown",
            (".pdf", "application/pdf") => "application/pdf",
            _ => throw new InvalidDataException("Only plain text, Markdown, and PDF files are supported.")
        };
    }

    private static async Task<byte[]> ReadBoundedAsync(
        Stream content,
        int maximumBytes,
        CancellationToken cancellationToken)
    {
        await using var buffer = new MemoryStream();
        var chunk = new byte[81920];
        while (true)
        {
            var read = await content.ReadAsync(chunk, cancellationToken);
            if (read == 0)
            {
                break;
            }

            if (buffer.Length + read > maximumBytes)
            {
                throw new InvalidDataException($"Files cannot exceed {maximumBytes} bytes.");
            }

            await buffer.WriteAsync(chunk.AsMemory(0, read), cancellationToken);
        }

        return buffer.ToArray();
    }

    private static void ValidateSignature(string contentType, byte[] bytes)
    {
        if (contentType == "application/pdf")
        {
            if (bytes.Length < 5
                || !bytes.AsSpan(0, 5).SequenceEqual("%PDF-"u8))
            {
                throw new InvalidDataException("The uploaded file does not have a valid PDF signature.");
            }

            return;
        }

        try
        {
            _ = new UTF8Encoding(encoderShouldEmitUTF8Identifier: false, throwOnInvalidBytes: true)
                .GetString(bytes);
        }
        catch (DecoderFallbackException exception)
        {
            throw new InvalidDataException("Text files must use valid UTF-8 encoding.", exception);
        }
    }
}

public sealed record ConversationFileOptions(int MaximumBytes = 10 * 1024 * 1024);
