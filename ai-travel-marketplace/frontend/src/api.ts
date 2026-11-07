import type {
  Activity,
  AssistantReply,
  Booking,
  City,
  Flight,
  Hotel,
  Quote,
  User,
} from './types'

const API_BASE = '/api'

export class ApiError extends Error {
  status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

async function request<T>(
  path: string,
  options: RequestInit = {},
  token?: string,
): Promise<T> {
  const headers = new Headers(options.headers)
  if (options.body) headers.set('Content-Type', 'application/json')
  if (token) headers.set('Authorization', `Bearer ${token}`)

  let response: Response
  try {
    response = await fetch(`${API_BASE}${path}`, { ...options, headers })
  } catch {
    throw new ApiError('We could not reach Atlas. Check that the backend is running.', 0)
  }

  if (!response.ok) {
    let message = `Request failed (${response.status})`
    try {
      const body = (await response.json()) as { detail?: string }
      if (body.detail) message = body.detail
    } catch {
      // Some upstream services return an empty response body.
    }
    throw new ApiError(message, response.status)
  }

  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

function queryString(values: Record<string, string | number | undefined>): string {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(values)) {
    if (value !== undefined && value !== '') params.set(key, String(value))
  }
  return params.toString()
}

export const api = {
  cities: (query = '') =>
    request<City[]>(`/destination/cities${query ? `?${queryString({ query })}` : ''}`),
  activities: (cityCode: string, date: string) =>
    request<Activity[]>(
      `/activities/search?${queryString({ city_code: cityCode, activity_date: date })}`,
    ),
  hotels: (cityCode: string, checkIn: string, checkOut: string, guests: number) =>
    request<Hotel[]>(
      `/hotels/search?${queryString({ city_code: cityCode, check_in: checkIn, check_out: checkOut, guests })}`,
    ),
  flights: (origin: string, destination: string, date: string, passengers: number) =>
    request<Flight[]>(
      `/flights/search?${queryString({ origin, destination, departure_date: date, passengers })}`,
    ),
  login: (email: string, password: string) =>
    request<{ access_token: string; token_type: string }>('/identity/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email, password }),
    }),
  register: (email: string, password: string, displayName: string) =>
    request<User>('/identity/auth/register', {
      method: 'POST',
      body: JSON.stringify({ email, password, display_name: displayName }),
    }),
  me: (token: string) => request<User>('/identity/users/me', {}, token),
  createQuote: (selection: Record<string, unknown>, token: string) =>
    request<Quote>(
      '/booking/quotes',
      { method: 'POST', body: JSON.stringify(selection) },
      token,
    ),
  createBooking: (quoteId: string, idempotencyKey: string, token: string) =>
    request<Booking>(
      '/booking/bookings',
      {
        method: 'POST',
        headers: { 'Idempotency-Key': idempotencyKey },
        body: JSON.stringify({ quote_id: quoteId }),
      },
      token,
    ),
  assistant: (
    message: string,
    conversationId: string | null,
    token: string,
  ) =>
    request<AssistantReply>(
      '/ai/assistant/chat',
      {
        method: 'POST',
        body: JSON.stringify({ message, conversation_id: conversationId }),
      },
      token,
    ),
  confirmAssistantQuote: (
    quoteId: string,
    confirmationPhrase: string,
    token: string,
  ) =>
    request<Booking>(
      '/ai/assistant/confirm',
      {
        method: 'POST',
        body: JSON.stringify({
          quote_id: quoteId,
          confirmation_phrase: confirmationPhrase,
        }),
      },
      token,
    ),
}
