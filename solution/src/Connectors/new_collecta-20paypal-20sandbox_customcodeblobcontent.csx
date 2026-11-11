using System;
using System.Net;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Text;
using System.Threading.Tasks;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

/// <summary>
/// Collecta - PayPal REST API (sandbox) custom connector code.
///
/// The connection uses Basic authentication (username = PayPal Client ID, password = PayPal Secret),
/// but the PayPal REST API expects an OAuth 2.0 bearer token. On every request this script:
///   1. reads the incoming "Authorization: Basic ..." header,
///   2. exchanges it for an access token (client_credentials grant),
///   3. replaces the header with "Authorization: Bearer {token}",
///   4. forwards the original request and returns the PayPal response unchanged (errors included).
///
/// No credentials are stored here: they only ever come from the connection.
/// Custom code has a short execution limit, so the script makes exactly two HTTP calls and never
/// parses or buffers the PayPal response of the forwarded request.
/// </summary>
public class Script : ScriptBase
{
    private const string TokenUrl = "https://api-m.sandbox.paypal.com/v1/oauth2/token";

    public override async Task<HttpResponseMessage> ExecuteAsync()
    {
        HttpRequestMessage request = this.Context.Request;

        // 1. The connector runtime builds this header from the connection (Client ID / Secret).
        AuthenticationHeaderValue basic = request.Headers.Authorization;
        if (basic == null
            || !string.Equals(basic.Scheme, "Basic", StringComparison.OrdinalIgnoreCase)
            || string.IsNullOrEmpty(basic.Parameter))
        {
            return CreateError(
                HttpStatusCode.Unauthorized,
                "AUTHENTICATION_FAILURE",
                "The connection did not supply a Basic Authorization header. Recreate the connection with the PayPal Client ID and Secret.");
        }

        // 2. Exchange the Client ID / Secret for an access token.
        var tokenRequest = new HttpRequestMessage(HttpMethod.Post, TokenUrl);
        tokenRequest.Headers.Authorization = new AuthenticationHeaderValue("Basic", basic.Parameter);
        tokenRequest.Headers.Accept.Add(new MediaTypeWithQualityHeaderValue("application/json"));
        tokenRequest.Content = new StringContent("grant_type=client_credentials", Encoding.UTF8, "application/x-www-form-urlencoded");

        HttpResponseMessage tokenResponse = await this.Context.SendAsync(tokenRequest, this.CancellationToken).ConfigureAwait(false);
        if (!tokenResponse.IsSuccessStatusCode)
        {
            // Wrong Client ID / Secret, etc. Return the PayPal error unchanged.
            return tokenResponse;
        }

        string accessToken = null;
        try
        {
            string tokenJson = await tokenResponse.Content.ReadAsStringAsync().ConfigureAwait(false);
            accessToken = (string)JObject.Parse(tokenJson)["access_token"];
        }
        catch (JsonException)
        {
            // Handled below: accessToken stays null.
        }

        if (string.IsNullOrEmpty(accessToken))
        {
            return CreateError(
                HttpStatusCode.BadGateway,
                "TOKEN_RESPONSE_INVALID",
                "The PayPal token endpoint did not return an access_token.");
        }

        // 3. Swap Basic for Bearer.
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", accessToken);

        if (request.Method == HttpMethod.Post)
        {
            // PayPal answers 415 UNSUPPORTED_MEDIA_TYPE to a POST without a JSON content type. Operations whose
            // body is optional or absent (send, remind, cancel, capture, generate number) may arrive without one.
            if (await IsEmptyAsync(request.Content).ConfigureAwait(false))
            {
                request.Content = new StringContent("{}", Encoding.UTF8, "application/json");
            }

            // Ask PayPal for the complete resource on create calls, so the response matches the connector schema.
            if (IsCreateCall(request.RequestUri) && !request.Headers.Contains("Prefer"))
            {
                request.Headers.TryAddWithoutValidation("Prefer", "return=representation");
            }
        }

        // 4. Forward the original request. Success and error responses are returned as PayPal sent them.
        return await this.Context.SendAsync(request, this.CancellationToken).ConfigureAwait(false);
    }

    /// <summary>
    /// CreateDraftInvoice, CreateOrder and CaptureOrder. Matched on the request path rather than on
    /// Context.OperationId, because the operation ID arrives base64 encoded in some regions.
    /// </summary>
    private static bool IsCreateCall(Uri uri)
    {
        string path = uri.AbsolutePath.TrimEnd('/');
        const StringComparison cmp = StringComparison.OrdinalIgnoreCase;

        return path.EndsWith("/v2/invoicing/invoices", cmp)
            || path.EndsWith("/v2/checkout/orders", cmp)
            || (path.IndexOf("/v2/checkout/orders/", cmp) >= 0 && path.EndsWith("/capture", cmp));
    }

    private static async Task<bool> IsEmptyAsync(HttpContent content)
    {
        if (content == null)
        {
            return true;
        }

        long? length = content.Headers.ContentLength;
        if (length.HasValue)
        {
            return length.Value == 0;
        }

        byte[] bytes = await content.ReadAsByteArrayAsync().ConfigureAwait(false);
        return bytes.Length == 0;
    }

    /// <summary>Errors raised by the script itself use the same shape as PayPal errors (name / message).</summary>
    private static HttpResponseMessage CreateError(HttpStatusCode status, string name, string message)
    {
        var response = new HttpResponseMessage(status);
        response.Content = CreateJsonContent(new JObject
        {
            ["name"] = name,
            ["message"] = message,
        }.ToString());
        return response;
    }
}
