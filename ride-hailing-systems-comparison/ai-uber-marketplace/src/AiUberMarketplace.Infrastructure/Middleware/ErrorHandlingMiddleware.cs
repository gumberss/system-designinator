using System.Net;
using System.Text.Json.Serialization;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.Logging;

namespace AiUberMarketplace.Infrastructure.Middleware
{
    /// <summary>
    /// Global exception handling middleware
    /// </summary>
    public class ErrorHandlingMiddleware
    {
        private readonly RequestDelegate _next;
        private readonly ILogger<ErrorHandlingMiddleware> _logger;

        public ErrorHandlingMiddleware(RequestDelegate next, ILogger<ErrorHandlingMiddleware> logger)
        {
            _next = next;
            _logger = logger;
        }

        public async Task InvokeAsync(HttpContext context)
        {
            try
            {
                await _next(context);
            }
            catch (Exception ex)
            {
                await HandleExceptionAsync(context, ex);
            }
        }

        private static Task HandleExceptionAsync(HttpContext context, Exception exception)
        {
            context.Response.ContentType = "application/json";

            var response = new ErrorResponse
            {
                Message = exception.Message,
                StatusCode = HttpStatusCode.InternalServerError
            };

            switch (exception)
            {
                case ArgumentNullException argNull:
                    response.StatusCode = HttpStatusCode.BadRequest;
                    response.Message = $"Required field is missing: {argNull.ParamName}";
                    context.Response.StatusCode = (int)HttpStatusCode.BadRequest;
                    break;

                case ArgumentException arg:
                    response.StatusCode = HttpStatusCode.BadRequest;
                    response.Message = arg.Message;
                    context.Response.StatusCode = (int)HttpStatusCode.BadRequest;
                    break;

                case InvalidOperationException invalid:
                    response.StatusCode = HttpStatusCode.BadRequest;
                    response.Message = invalid.Message;
                    context.Response.StatusCode = (int)HttpStatusCode.BadRequest;
                    break;

                case KeyNotFoundException keyNotFound:
                    response.StatusCode = HttpStatusCode.NotFound;
                    response.Message = keyNotFound.Message;
                    context.Response.StatusCode = (int)HttpStatusCode.NotFound;
                    break;

                default:
                    context.Response.StatusCode = (int)HttpStatusCode.InternalServerError;
                    break;
            }

            return context.Response.WriteAsJsonAsync(response);
        }

        private class ErrorResponse
        {
            [JsonPropertyName("statusCode")]
            public HttpStatusCode StatusCode { get; set; }

            [JsonPropertyName("message")]
            public required string Message { get; set; }

            [JsonPropertyName("timestamp")]
            public DateTime Timestamp { get; set; } = DateTime.UtcNow;
        }
    }
}
