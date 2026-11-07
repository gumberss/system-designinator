using System.Text;
using AiChatinator.Application;
using UglyToad.PdfPig;

namespace AiChatinator.Infrastructure;

public sealed class PdfPigTextExtractor : IFileTextExtractor
{
    public async Task<string> ExtractTextAsync(
        string fileName,
        string contentType,
        Stream content,
        CancellationToken cancellationToken)
    {
        if (contentType != "application/pdf")
        {
            using var reader = new StreamReader(
                content,
                new UTF8Encoding(encoderShouldEmitUTF8Identifier: false, throwOnInvalidBytes: true),
                detectEncodingFromByteOrderMarks: true,
                leaveOpen: true);
            return await reader.ReadToEndAsync(cancellationToken);
        }

        try
        {
            using var document = PdfDocument.Open(content);
            var text = new StringBuilder();
            foreach (var page in document.GetPages())
            {
                cancellationToken.ThrowIfCancellationRequested();
                if (text.Length > 0)
                {
                    text.AppendLine();
                }

                text.Append(page.Text);
                if (text.Length > 40_000)
                {
                    break;
                }
            }

            return text.ToString();
        }
        catch (Exception exception) when (exception is not OperationCanceledException)
        {
            throw new InvalidDataException($"Could not extract text from PDF '{fileName}'.", exception);
        }
    }
}
