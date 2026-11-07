export type TripKind = 'destination' | 'hotel' | 'activity' | 'flight'

export interface City {
  code: string
  name: string
  country_code: string
}

export interface Destination {
  code: string
  name: string
  country_code: string
}

export interface Hotel {
  hotel_id: string
  hotel_name: string
  city_code: string
  rating: number
  room_type_id: string
  room_type: string
  max_guests: number
  price_per_night: number
  currency: string
  available_units: number
}

export interface Activity {
  activity_id: string
  name: string
  city_code: string
  description: string
  duration_minutes: number
  schedule_id: string
  schedule_date: string
  remaining_capacity: number
  price_per_person: number
  currency: string
}

export interface Flight {
  id: string
  airline_code: string
  airline_name: string
  origin_city_code: string
  destination_city_code: string
  departure_at: string
  arrival_at: string
  price: number
  currency: string
  seats_available: number
}

export type SearchItem = Destination | Hotel | Activity | Flight

export interface User {
  id: string
  email: string
  display_name: string
  preferences: Record<string, unknown>
}

export interface Quote {
  id: string
  total_amount: number
  currency: string
  expires_at: string
  selection: Record<string, unknown>
}

export interface Booking {
  id: string
  quote_id: string
  status: string
  total_amount: number
  currency: string
  failure_reason: string | null
  reservation_ids: Record<string, string>
}

export interface AssistantReply {
  conversation_id: string
  answer: string
  citations: { title: string; source: string }[]
  pending_quote: {
    quote_id: string
    confirmation_phrase: string
    total_amount: number
    currency: string
    expires_at: string
  } | null
}
