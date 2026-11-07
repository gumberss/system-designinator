import { AnimatePresence, motion } from 'motion/react'
import {
  ArrowDown,
  ArrowRight,
  ArrowUpRight,
  CalendarDays,
  Check,
  ChevronDown,
  Compass,
  Globe2,
  Heart,
  Hotel as HotelIcon,
  LoaderCircle,
  LockKeyhole,
  MapPin,
  Menu,
  Plane,
  Search,
  Send,
  Sparkles,
  Star,
  Ticket,
  X,
} from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from './api'
import type {
  Activity,
  AssistantReply,
  Booking,
  City,
  Destination,
  Flight,
  Hotel,
  Quote,
  SearchItem,
  TripKind,
  User,
} from './types'

const TOKEN_KEY = 'atlas_access_token'
const TRIPS_KEY = 'atlas_recent_trips'
const CONVERSATION_KEY = 'atlas_conversation_id'
const CITY_PHOTOS: Record<string, string> = {
  TYO: 'photo-1493976040374-85c8e12f0c0e',
  KYO: 'photo-1493976040374-85c8e12f0c0e',
  OSA: 'photo-1590559899731-a382839e5549',
  PAR: 'photo-1502602898657-3e91760cbb34',
  ROM: 'photo-1515542622106-78bda8ba0e5b',
  NYC: 'photo-1534430480872-3498386e7856',
  LIS: 'photo-1555881400-74d7acaacd8b',
  MRC: 'photo-1539020140153-e479b8c22e70',
  default: 'photo-1464822759023-fed622ff2c3b',
}

const tabs: { id: TripKind; label: string; icon: typeof Compass }[] = [
  { id: 'destination', label: 'Explore', icon: Compass },
  { id: 'hotel', label: 'Stays', icon: HotelIcon },
  { id: 'flight', label: 'Flights', icon: Plane },
  { id: 'activity', label: 'Experiences', icon: Ticket },
]

function localDate(offset: number): string {
  const date = new Date()
  date.setDate(date.getDate() + offset)
  return date.toISOString().slice(0, 10)
}

function imageFor(cityCode?: string): string {
  const id = CITY_PHOTOS[cityCode ?? ''] ?? CITY_PHOTOS.default
  return `https://images.unsplash.com/${id}?auto=format&fit=crop&w=960&q=85`
}

function titleCase(value: string): string {
  return value
    .replaceAll('_', ' ')
    .replace(/\b\w/g, (letter) => letter.toUpperCase())
}

function money(amount: number, currency = 'USD'): string {
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency,
    maximumFractionDigits: 0,
  }).format(amount)
}

function cityName(cities: City[], code?: string): string {
  return cities.find((city) => city.code === code)?.name ?? code ?? 'Somewhere'
}

function kindName(kind: TripKind): string {
  return { destination: 'destination', hotel: 'stay', flight: 'flight', activity: 'experience' }[kind]
}

function getErrorMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'Something went wrong. Please try again.'
}

function isHotel(item: SearchItem): item is Hotel {
  return 'hotel_id' in item
}

function isActivity(item: SearchItem): item is Activity {
  return 'activity_id' in item
}

function isFlight(item: SearchItem): item is Flight {
  return 'airline_name' in item
}

function isDestination(item: SearchItem): item is Destination {
  return 'code' in item && 'country_code' in item
}

function resultTitle(item: SearchItem, cities: City[]): string {
  if (isHotel(item)) return item.hotel_name
  if (isActivity(item)) return item.name
  if (isFlight(item)) return `${item.airline_name} · ${cityName(cities, item.origin_city_code)} to ${cityName(cities, item.destination_city_code)}`
  return item.name
}

function resultCity(item: SearchItem, cities: City[]): string {
  if (isFlight(item)) return `${cityName(cities, item.origin_city_code)} → ${cityName(cities, item.destination_city_code)}`
  return cityName(cities, isDestination(item) ? item.code : item.city_code)
}

function resultPrice(item: SearchItem): { amount: number; currency: string; suffix: string } | null {
  if (isHotel(item)) return { amount: Number(item.price_per_night), currency: item.currency, suffix: '/ night' }
  if (isActivity(item)) return { amount: Number(item.price_per_person), currency: item.currency, suffix: '/ person' }
  if (isFlight(item)) return { amount: Number(item.price), currency: item.currency, suffix: '/ person' }
  return null
}

function resultMeta(item: SearchItem): string {
  if (isHotel(item)) return `${item.room_type} · sleeps ${item.max_guests}`
  if (isActivity(item)) return `${Math.round(item.duration_minutes / 60 * 10) / 10} hours · ${item.remaining_capacity} spots left`
  if (isFlight(item)) return `${new Date(item.departure_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })} · ${new Date(item.departure_at).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' })}`
  return `${item.country_code} · a place to get lost`
}

function App() {
  const [kind, setKind] = useState<TripKind>('destination')
  const [cities, setCities] = useState<City[]>([])
  const [selectedCity, setSelectedCity] = useState('')
  const [origin, setOrigin] = useState('')
  const [departureDate, setDepartureDate] = useState(localDate(8))
  const [returnDate, setReturnDate] = useState(localDate(10))
  const [partySize, setPartySize] = useState(2)
  const [items, setItems] = useState<SearchItem[]>([])
  const [searched, setSearched] = useState(false)
  const [loading, setLoading] = useState(false)
  const [pageError, setPageError] = useState('')
  const [token, setToken] = useState(() => localStorage.getItem(TOKEN_KEY) ?? '')
  const [user, setUser] = useState<User | null>(null)
  const [authOpen, setAuthOpen] = useState(false)
  const [authMode, setAuthMode] = useState<'login' | 'register'>('login')
  const [authError, setAuthError] = useState('')
  const [authBusy, setAuthBusy] = useState(false)
  const [quote, setQuote] = useState<Quote | null>(null)
  const [bookingBusy, setBookingBusy] = useState(false)
  const [bookingResult, setBookingResult] = useState<Booking | null>(null)
  const [chatOpen, setChatOpen] = useState(false)
  const [chatBusy, setChatBusy] = useState(false)
  const [chatError, setChatError] = useState('')
  const [assistantQuote, setAssistantQuote] = useState<AssistantReply['pending_quote']>(null)
  const [conversationId, setConversationId] = useState(() => localStorage.getItem(CONVERSATION_KEY))
  const [chatMessages, setChatMessages] = useState<{ role: 'assistant' | 'user'; text: string }[]>([
    { role: 'assistant', text: 'Hi, I’m your Atlas travel companion. Tell me what kind of trip you’re dreaming about.' },
  ])
  const [activeNav, setActiveNav] = useState<'explore' | 'trips'>('explore')
  const [recentTrips, setRecentTrips] = useState<Booking[]>(() => {
    try {
      return JSON.parse(localStorage.getItem(TRIPS_KEY) ?? '[]') as Booking[]
    } catch {
      return []
    }
  })
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false)
  const chatInput = useRef<HTMLInputElement>(null)

  useEffect(() => {
    let ignore = false
    api.cities()
      .then((response) => {
        if (ignore) return
        setCities(response)
        const preferred = response.find((city) => city.code === 'TYO') ?? response[0]
        if (preferred) {
          setSelectedCity(preferred.code)
          setOrigin(preferred.code)
        }
      })
      .catch((error: unknown) => {
        if (!ignore) setPageError(getErrorMessage(error))
      })
    return () => { ignore = true }
  }, [])

  useEffect(() => {
    if (!token) {
      setUser(null)
      return
    }
    let ignore = false
    api.me(token)
      .then((profile) => { if (!ignore) setUser(profile) })
      .catch(() => {
        if (!ignore) {
          localStorage.removeItem(TOKEN_KEY)
          setToken('')
          setUser(null)
        }
      })
    return () => { ignore = true }
  }, [token])

  const displayedItems = useMemo(() => items.slice(0, 8), [items])
  const selectedCityName = cityName(cities, selectedCity)

  const searchTrips = useCallback(async () => {
    setPageError('')
    setLoading(true)
    setSearched(true)
    setItems([])
    try {
      let results: SearchItem[]
      if (kind === 'destination') {
        results = await api.cities(selectedCityName === 'Somewhere' ? '' : selectedCityName)
      } else if (kind === 'hotel') {
        results = await api.hotels(selectedCity, departureDate, returnDate, partySize)
      } else if (kind === 'activity') {
        results = await api.activities(selectedCity, departureDate)
      } else {
        results = await api.flights(origin, selectedCity, departureDate, partySize)
      }
      setItems(results)
    } catch (error) {
      setPageError(getErrorMessage(error))
    } finally {
      setLoading(false)
    }
  }, [departureDate, kind, origin, partySize, returnDate, selectedCity, selectedCityName])

  async function submitAuth(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    const email = String(form.get('email') ?? '').trim()
    const password = String(form.get('password') ?? '')
    const displayName = String(form.get('displayName') ?? '').trim()
    setAuthError('')
    setAuthBusy(true)
    try {
      if (authMode === 'register') await api.register(email, password, displayName)
      const result = await api.login(email, password)
      localStorage.setItem(TOKEN_KEY, result.access_token)
      setToken(result.access_token)
      setAuthOpen(false)
    } catch (error) {
      setAuthError(getErrorMessage(error))
    } finally {
      setAuthBusy(false)
    }
  }

  function signOut() {
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(CONVERSATION_KEY)
    setToken('')
    setUser(null)
    setConversationId(null)
    setAssistantQuote(null)
    setChatMessages([
      { role: 'assistant', text: 'Hi, I’m your Atlas travel companion. Tell me what kind of trip you’re dreaming about.' },
    ])
  }

  function selectionFor(item: SearchItem): Record<string, unknown> {
    if (isHotel(item)) {
      return {
        hotel: {
          hotel_id: item.hotel_id,
          room_type_id: item.room_type_id,
          city_code: item.city_code,
          check_in: departureDate,
          check_out: returnDate,
          units: 1,
          guests: partySize,
        },
      }
    }
    if (isActivity(item)) {
      return {
        activity: {
          schedule_id: item.schedule_id,
          city_code: item.city_code,
          activity_date: item.schedule_date,
          participants: partySize,
        },
      }
    }
    if (isFlight(item)) {
      return {
        flight: {
          offer_id: item.id,
          origin: item.origin_city_code,
          destination: item.destination_city_code,
          departure_date: item.departure_at.slice(0, 10),
          passengers: partySize,
        },
      }
    }
    return {}
  }

  async function beginBooking(item: SearchItem) {
    if (!token) {
      setAuthMode('login')
      setAuthOpen(true)
      setAuthError('Sign in to continue to a secure quote.')
      return
    }
    setBookingBusy(true)
    setPageError('')
    try {
      const result = await api.createQuote(selectionFor(item), token)
      setQuote(result)
    } catch (error) {
      setPageError(getErrorMessage(error))
    } finally {
      setBookingBusy(false)
    }
  }

  async function confirmBooking() {
    if (!quote || !token) return
    setBookingBusy(true)
    try {
      const result = await api.createBooking(
        quote.id,
        `atlas-${quote.id}`,
        token,
      )
      setBookingResult(result)
      const nextTrips = [result, ...recentTrips.filter((trip) => trip.id !== result.id)]
      setRecentTrips(nextTrips)
      localStorage.setItem(TRIPS_KEY, JSON.stringify(nextTrips))
    } catch (error) {
      setPageError(getErrorMessage(error))
      setQuote(null)
    } finally {
      setBookingBusy(false)
    }
  }

  async function sendChat(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const message = chatInput.current?.value.trim()
    if (!message) return
    if (!token) {
      setChatError('Sign in to chat with your travel companion.')
      setAuthOpen(true)
      return
    }
    setChatError('')
    setChatMessages((current) => [...current, { role: 'user', text: message }])
    if (chatInput.current) chatInput.current.value = ''
    setChatBusy(true)
    try {
      const response: AssistantReply = await api.assistant(message, conversationId, token)
      setConversationId(response.conversation_id)
      localStorage.setItem(CONVERSATION_KEY, response.conversation_id)
      setAssistantQuote(response.pending_quote)
      setChatMessages((current) => [...current, { role: 'assistant', text: response.answer }])
    } catch (error) {
      setChatError(getErrorMessage(error))
    } finally {
      setChatBusy(false)
      chatInput.current?.focus()
    }
  }

  async function confirmAssistantBooking() {
    if (!assistantQuote || !token) return
    setChatBusy(true)
    setChatError('')
    try {
      const result = await api.confirmAssistantQuote(
        assistantQuote.quote_id,
        assistantQuote.confirmation_phrase,
        token,
      )
      const nextTrips = [result, ...recentTrips.filter((trip) => trip.id !== result.id)]
      setRecentTrips(nextTrips)
      localStorage.setItem(TRIPS_KEY, JSON.stringify(nextTrips))
      setAssistantQuote(null)
      setChatMessages((current) => [
        ...current,
        { role: 'assistant', text: `Your booking is ${titleCase(result.status).toLowerCase()}. I’ve saved it to My trips.` },
      ])
    } catch (error) {
      setChatError(getErrorMessage(error))
    } finally {
      setChatBusy(false)
    }
  }

  function activateTab(nextKind: TripKind) {
    setKind(nextKind)
    setSearched(false)
    setItems([])
    setPageError('')
  }

  const destinationCards = useMemo(() => {
    const byCode = new Map(cities.map((city) => [city.code, city]))
    const popularCodes = ['TYO', 'PAR', 'ROM', 'LIS']
    const popular = popularCodes
      .map((code) => byCode.get(code))
      .filter((city): city is City => city !== undefined)
    return popular.length ? popular : cities.slice(0, 4)
  }, [cities])

  return (
    <div className="app-shell">
      <header className="topbar">
        <a className="brand" href="#" aria-label="Atlas home" onClick={() => setActiveNav('explore')}>
          <span className="brand-mark"><Compass size={21} strokeWidth={2.2} /></span>
          <span>atlas<span className="brand-dot">.</span></span>
        </a>
        <nav className={`main-nav ${mobileMenuOpen ? 'is-open' : ''}`} aria-label="Main navigation">
          <button className={activeNav === 'explore' ? 'nav-link active' : 'nav-link'} onClick={() => { setActiveNav('explore'); setMobileMenuOpen(false) }}>Explore</button>
          <button className={activeNav === 'trips' ? 'nav-link active' : 'nav-link'} onClick={() => { setActiveNav('trips'); setMobileMenuOpen(false) }}>My trips{recentTrips.length > 0 && <span className="nav-count">{recentTrips.length}</span>}</button>
          <button className="nav-link nav-concierge" onClick={() => { setChatOpen(true); setMobileMenuOpen(false) }}><Sparkles size={15} /> AI concierge</button>
        </nav>
        <div className="top-actions">
          <button className="icon-button mobile-menu" aria-label="Toggle menu" onClick={() => setMobileMenuOpen((open) => !open)}>{mobileMenuOpen ? <X size={20} /> : <Menu size={20} />}</button>
          {user ? (
            <button className="profile-button" onClick={signOut} title="Sign out">
              <span className="avatar">{user.display_name.slice(0, 1).toUpperCase()}</span>
              <span className="profile-name">{user.display_name.split(' ')[0]}</span>
              <ChevronDown size={15} />
            </button>
          ) : (
            <button className="sign-in-button" onClick={() => { setAuthMode('login'); setAuthOpen(true) }}>Sign in <ArrowUpRight size={15} /></button>
          )}
        </div>
      </header>

      <main>
        {activeNav === 'trips' ? (
          <section className="trips-page page-wrap">
            <p className="eyebrow"><span className="eyebrow-line" /> YOUR ATLAS</p>
            <h1 className="section-title">The places you’re <span>going.</span></h1>
            <p className="section-subtitle">Your plans, all in one place. Every great trip starts somewhere.</p>
            {!user && <div className="inline-notice"><LockKeyhole size={18} /><span>Sign in to see bookings linked to your account.</span><button onClick={() => setAuthOpen(true)}>Sign in</button></div>}
            {recentTrips.length ? (
              <div className="trips-list">{recentTrips.map((trip) => (
                <article className="trip-row" key={trip.id}>
                  <span className="trip-icon"><Ticket size={21} /></span>
                  <div><strong>Booking {trip.id.slice(0, 8)}</strong><span>{titleCase(trip.status)} · {money(Number(trip.total_amount), trip.currency)}</span></div>
                  <ArrowUpRight size={18} />
                </article>
              ))}</div>
            ) : (
              <div className="empty-trips">
                <div className="empty-globe"><Globe2 size={32} /></div>
                <h2>Your next story is waiting.</h2>
                <p>Find a place that moves you. We’ll keep your plans right here.</p>
                <button className="primary-button" onClick={() => setActiveNav('explore')}>Explore places <ArrowRight size={17} /></button>
              </div>
            )}
          </section>
        ) : (
          <>
            <section className="hero-section">
              <div className="hero-copy">
                <p className="eyebrow hero-eyebrow"><span className="eyebrow-line" /> GO A LITTLE FURTHER</p>
                <h1>Find your<br /><span>elsewhere.</span></h1>
                <p className="hero-description">The world is big. Your next chapter is out there.<br className="desktop-break" /> Let’s find the place that feels like you.</p>
                <button className="hero-link" onClick={() => document.getElementById('discover')?.scrollIntoView({ behavior: 'smooth' })}>See what’s out there <ArrowDown size={16} /></button>
              </div>
              <div className="hero-photo">
                <img src="https://images.unsplash.com/photo-1476514525535-07fb3b4ae5f1?auto=format&fit=crop&w=1800&q=90" alt="A quiet alpine lake surrounded by mountains" />
                <div className="photo-tag"><span className="photo-tag-dot" /> THE DOLOMITES, ITALY</div>
                <div className="hero-stamp"><span>MAKE</span><span>ROOM</span><span>FOR <i>WONDER</i></span></div>
              </div>
              <span className="hero-index">01 — 04</span>
            </section>

            <section className="search-section" aria-label="Search trips">
              <div className="search-tabs" role="tablist" aria-label="Trip type">
                {tabs.map(({ id, label, icon: Icon }) => (
                  <button
                    className={kind === id ? 'search-tab selected' : 'search-tab'}
                    key={id}
                    onClick={() => activateTab(id)}
                    role="tab"
                    aria-selected={kind === id}
                  >
                    <Icon size={17} strokeWidth={1.8} /><span>{label}</span>
                  </button>
                ))}
              </div>
              <div className={`search-fields ${kind === 'flight' ? 'flight-fields' : ''}`}>
                {kind === 'flight' && (
                  <label className="search-field">
                    <span className="field-label">FROM</span>
                    <span className="field-input-wrap"><MapPin size={16} /><select value={origin} onChange={(event) => setOrigin(event.target.value)} aria-label="Origin city">{cities.map((city) => <option key={city.code} value={city.code}>{city.name}</option>)}</select></span>
                  </label>
                )}
                <label className="search-field">
                  <span className="field-label">{kind === 'flight' ? 'TO' : 'WHERE TO?'}</span>
                  <span className="field-input-wrap"><MapPin size={16} /><select value={selectedCity} onChange={(event) => setSelectedCity(event.target.value)} aria-label="Destination city">{cities.map((city) => <option key={city.code} value={city.code}>{city.name}</option>)}</select></span>
                </label>
                {kind !== 'destination' && (
                  <label className="search-field">
                    <span className="field-label">{kind === 'hotel' ? 'CHECK IN' : 'WHEN?'}</span>
                    <span className="field-input-wrap"><CalendarDays size={16} /><input type="date" min={localDate(1)} value={departureDate} onChange={(event) => setDepartureDate(event.target.value)} aria-label="Start date" /></span>
                  </label>
                )}
                {kind === 'hotel' && (
                  <label className="search-field">
                    <span className="field-label">CHECK OUT</span>
                    <span className="field-input-wrap"><CalendarDays size={16} /><input type="date" min={departureDate} value={returnDate} onChange={(event) => setReturnDate(event.target.value)} aria-label="End date" /></span>
                  </label>
                )}
                {kind !== 'destination' && (
                  <label className="search-field guests-field">
                    <span className="field-label">{kind === 'activity' ? 'TRAVELERS' : kind === 'flight' ? 'PASSENGERS' : 'GUESTS'}</span>
                    <span className="field-input-wrap"><Globe2 size={16} /><select value={partySize} onChange={(event) => setPartySize(Number(event.target.value))} aria-label="Travelers">{Array.from({ length: 8 }, (_, index) => index + 1).map((count) => <option key={count} value={count}>{count} {count === 1 ? 'traveler' : 'travelers'}</option>)}</select></span>
                  </label>
                )}
                <button className="search-submit" onClick={searchTrips} disabled={loading || cities.length === 0}>
                  {loading ? <LoaderCircle className="spin" size={19} /> : <Search size={19} />}
                  <span>{loading ? 'Finding…' : 'Find my trip'}</span>
                </button>
              </div>
              <p className="search-note"><LockKeyhole size={12} /> No booking fees. Just the good stuff.</p>
            </section>

            {pageError && <div className="page-alert page-wrap" role="alert"><span>{pageError}</span><button aria-label="Dismiss error" onClick={() => setPageError('')}><X size={17} /></button></div>}

            {searched && (
              <section className="results-section page-wrap" aria-live="polite">
                <div className="section-heading">
                  <div><p className="eyebrow"><span className="eyebrow-line" /> YOUR SEARCH</p><h2 className="section-title">{displayedItems.length ? `${displayedItems.length} ${kindName(kind)}${displayedItems.length === 1 ? '' : 's'} to fall for.` : `No ${kindName(kind)}s found just yet.`}</h2></div>
                  <button className="clear-search" onClick={() => { setSearched(false); setItems([]) }}>Clear search <X size={15} /></button>
                </div>
                {loading ? <div className="loading-row"><LoaderCircle className="spin" /> Finding your next favorite place…</div> : (
                  <div className="result-grid">{displayedItems.map((item, index) => (
                    <ResultCard
                      key={('id' in item ? item.id : isHotel(item) ? item.hotel_id + item.room_type_id : isActivity(item) ? item.schedule_id : isFlight(item) ? `${item.id}-${index}` : `${item.code}-${index}`)}
                      item={item}
                      index={index}
                      cities={cities}
                      onBook={beginBooking}
                      bookingBusy={bookingBusy}
                    />
                  ))}</div>
                )}
                {!loading && displayedItems.length === 0 && <EmptyResults kind={kind} onChangeKind={activateTab} />}
              </section>
            )}

            <section className="discover-section page-wrap" id="discover">
              <div className="section-heading">
                <div><p className="eyebrow"><span className="eyebrow-line" /> THE WORLD, YOUR WAY</p><h2 className="section-title">A few places to <span>begin.</span></h2></div>
                <button className="text-link" onClick={() => { setKind('destination'); setSearched(true); void api.cities().then(setItems).catch((error: unknown) => setPageError(getErrorMessage(error))) }}>All destinations <ArrowRight size={16} /></button>
              </div>
              <div className="destination-grid">
                {destinationCards.map((city, index) => (
                  <motion.button
                    className={`destination-card destination-card-${index + 1}`}
                    key={city.code}
                    onClick={() => { setSelectedCity(city.code); setKind('destination'); void api.cities(city.name).then((result) => { setItems(result); setSearched(true); document.querySelector('.results-section')?.scrollIntoView({ behavior: 'smooth', block: 'start' }) }).catch((error: unknown) => setPageError(getErrorMessage(error))) }}
                    whileHover={{ y: -5 }}
                    transition={{ duration: 0.2 }}
                  >
                    <img src={imageFor(city.code)} alt={`${city.name} cityscape`} loading="lazy" />
                    <span className="destination-overlay" />
                    <span className="destination-info">                    <span>{city.country_code}</span><strong>{city.name}</strong><span className="destination-discover">Discover <ArrowUpRight size={14} /></span></span>
                    <span className="destination-number">0{index + 1}</span>
                  </motion.button>
                ))}
                {destinationCards.length === 0 && <div className="loading-row"><LoaderCircle className="spin" /> Loading places worth getting lost in…</div>}
              </div>
            </section>

            <section className="manifesto">
              <div className="manifesto-left"><span className="manifesto-kicker">TRAVEL, BUT MAKE IT YOURS</span><span className="manifesto-number">02 / 04</span></div>
              <div className="manifesto-content"><p>Not a checklist.<br />Not a <i>package.</i></p><span>Just the right place, at the right pace, for the story only you can tell.</span></div>
              <div className="manifesto-orbit"><Compass size={45} strokeWidth={1} /><span>WANDER<br />WELL</span></div>
            </section>

            <section className="bottom-cta page-wrap">
              <div className="bottom-cta-copy"><p className="eyebrow"><span className="eyebrow-line" /> A LITTLE HELP, A LOT OF HEART</p><h2>Big ideas.<br /><span>Better itineraries.</span></h2><p>Tell our AI concierge what you love. We’ll help shape a trip that feels like yours.</p><button className="primary-button" onClick={() => { setChatOpen(true); if (user) setTimeout(() => chatInput.current?.focus(), 100) }}>Meet your travel companion <Sparkles size={16} /></button></div>
              <div className="cta-art"><div className="cta-sun" /><div className="cta-card cta-card-back"><span>YOUR NEXT</span><strong>somewhere</strong><span>IS CLOSER THAN YOU THINK</span></div><div className="cta-card cta-card-front"><MapPin size={22} /><span>ON YOUR MAP</span><strong>Somewhere<br />wonderful.</strong><span>ATLAS TRAVEL NOTES · NO. 08</span></div><span className="cta-scribble">go on, then</span></div>
            </section>
          </>
        )}
      </main>

      <footer className="footer">
        <a className="brand footer-brand" href="#"><span className="brand-mark"><Compass size={18} /></span><span>atlas<span className="brand-dot">.</span></span></a>
        <span className="footer-note">Made for the places that stay with you.</span>
        <span className="footer-copy">© 2025 Atlas Travel Co.</span>
      </footer>

      <AnimatePresence>
        {authOpen && <Modal onClose={() => { setAuthOpen(false); setAuthError('') }}>
          <div className="auth-modal">
            <button className="modal-close" aria-label="Close dialog" onClick={() => { setAuthOpen(false); setAuthError('') }}><X size={19} /></button>
            <span className="modal-icon"><Compass size={23} /></span>
            <p className="eyebrow"><span className="eyebrow-line" /> YOUR NEXT CHAPTER</p>
            <h2>{authMode === 'login' ? 'Welcome back.' : 'Come on in.'}</h2>
            <p className="auth-description">{authMode === 'login' ? 'Your next great trip is just around the corner.' : 'Create an account to save the places you love.'}</p>
            <form onSubmit={submitAuth} className="auth-form">
              {authMode === 'register' && <label>Your name<input name="displayName" placeholder="How should we call you?" required maxLength={120} /></label>}
              <label>Email address<input name="email" type="email" placeholder="you@example.com" autoComplete="email" required minLength={5} maxLength={320} /></label>
              <label>Password<input name="password" type="password" placeholder="At least 12 characters" autoComplete={authMode === 'login' ? 'current-password' : 'new-password'} required minLength={authMode === 'register' ? 12 : 1} maxLength={256} /></label>
              {authError && <p className="form-error" role="alert">{authError}</p>}
              <button className="primary-button full-button" type="submit" disabled={authBusy}>{authBusy ? <LoaderCircle className="spin" size={18} /> : authMode === 'login' ? 'Sign in' : 'Create account'}{!authBusy && <ArrowRight size={17} />}</button>
            </form>
            <p className="auth-switch">{authMode === 'login' ? 'New around here?' : 'Already have an account?'} <button onClick={() => { setAuthMode(authMode === 'login' ? 'register' : 'login'); setAuthError('') }}>{authMode === 'login' ? 'Create an account' : 'Sign in'}</button></p>
            <span className="auth-privacy"><LockKeyhole size={12} /> Your details stay yours.</span>
          </div>
        </Modal>}
      </AnimatePresence>

      <AnimatePresence>
        {quote && <Modal onClose={() => setQuote(null)}>
          <div className="quote-modal">
            <button className="modal-close" aria-label="Close dialog" onClick={() => setQuote(null)}><X size={19} /></button>
            <span className="modal-icon quote-icon"><Check size={23} /></span>
            <p className="eyebrow"><span className="eyebrow-line" /> ONE LAST LOOK</p>
            <h2>Ready when<br /><span>you are.</span></h2>
            <p className="auth-description">Your quote is held for a little while. Review the total, then confirm to start your booking.</p>
            <div className="quote-summary"><span>YOUR TRIP TOTAL</span><strong>{money(Number(quote.total_amount), quote.currency)}</strong><span>Quote expires {new Date(quote.expires_at).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' })}</span></div>
            <div className="quote-note"><LockKeyhole size={15} /><span>Confirmation starts a simulated booking. Payment stays inside the demo.</span></div>
            <button className="primary-button full-button" onClick={confirmBooking} disabled={bookingBusy}>{bookingBusy ? <LoaderCircle className="spin" size={18} /> : 'Confirm and book'}{!bookingBusy && <ArrowRight size={17} />}</button>
            <button className="cancel-link" onClick={() => setQuote(null)}>Not yet, take me back</button>
          </div>
        </Modal>}
      </AnimatePresence>

      <AnimatePresence>
        {bookingResult && <Modal onClose={() => { setBookingResult(null); setQuote(null) }}>
          <div className="booking-success">
            <button className="modal-close" aria-label="Close dialog" onClick={() => { setBookingResult(null); setQuote(null) }}><X size={19} /></button>
            <span className="success-check"><Check size={28} /></span>
            <p className="eyebrow"><span className="eyebrow-line" /> IT’S HAPPENING</p>
            <h2>Your trip has<br /><span>a pulse.</span></h2>
            <p className="auth-description">Booking {bookingResult.id.slice(0, 8)} is {titleCase(bookingResult.status).toLowerCase()}. Find it anytime under My trips.</p>
            <div className="quote-summary"><span>BOOKING TOTAL</span><strong>{money(Number(bookingResult.total_amount), bookingResult.currency)}</strong></div>
            <button className="primary-button full-button" onClick={() => { setBookingResult(null); setQuote(null); setActiveNav('trips') }}>View my trips <ArrowRight size={17} /></button>
          </div>
        </Modal>}
      </AnimatePresence>

      <AnimatePresence>
        {chatOpen && <motion.aside className="chat-panel" initial={{ x: 420, opacity: 0 }} animate={{ x: 0, opacity: 1 }} exit={{ x: 420, opacity: 0 }} transition={{ type: 'spring', damping: 28, stiffness: 260 }} aria-label="AI travel concierge">
          <div className="chat-header"><div className="chat-avatar"><Sparkles size={18} /></div><div><strong>Your Atlas concierge</strong><span><i /> Here for the daydreamers</span></div><button className="icon-button" aria-label="Close concierge" onClick={() => setChatOpen(false)}><X size={19} /></button></div>
          <div className="chat-intro"><span className="chat-sun"><Sparkles size={15} /></span><span>THE ATLAS TRAVEL DESK · ALWAYS OPEN</span></div>
          <div className="chat-messages">
            {chatMessages.map((message, index) => <div className={`chat-message ${message.role}`} key={`${index}-${message.text.slice(0, 8)}`}><p>{message.text}</p></div>)}
            {chatBusy && <div className="chat-message assistant typing"><span /><span /><span /></div>}
            {chatError && <p className="chat-error" role="alert">{chatError}</p>}
          </div>
          {assistantQuote && <div className="assistant-quote"><span>YOUR QUOTE · EXPIRES {new Date(assistantQuote.expires_at).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' })}</span><strong>{money(Number(assistantQuote.total_amount), assistantQuote.currency)}</strong><button onClick={confirmAssistantBooking} disabled={chatBusy}>{chatBusy ? <LoaderCircle className="spin" size={16} /> : 'Confirm this trip'} <ArrowRight size={15} /></button><small>Your confirmation starts a simulated booking.</small></div>}
          <div className="chat-prompts"><span>NOT SURE WHERE TO START?</span><button onClick={() => { if (chatInput.current) chatInput.current.value = 'Plan a slow 5-day trip somewhere with good food and beautiful walks'; chatInput.current?.focus() }}>A slower kind of trip</button><button onClick={() => { if (chatInput.current) chatInput.current.value = 'What should I see in Kyoto if I only have one day?'; chatInput.current?.focus() }}>One day in Kyoto</button></div>
          <form className="chat-form" onSubmit={sendChat}><input ref={chatInput} aria-label="Message your travel concierge" placeholder="Tell me what you’re dreaming of…" disabled={chatBusy} /><button type="submit" aria-label="Send message" disabled={chatBusy}><Send size={17} /></button></form>
          <p className="chat-footnote"><LockKeyhole size={11} /> Your conversations are private and can be deleted anytime.</p>
        </motion.aside>}
      </AnimatePresence>
      {chatOpen && <button className="chat-backdrop" aria-label="Close concierge" onClick={() => setChatOpen(false)} />}
    </div>
  )
}

function ResultCard({
  item,
  index,
  cities,
  onBook,
  bookingBusy,
}: {
  item: SearchItem
  index: number
  cities: City[]
  onBook: (item: SearchItem) => void
  bookingBusy: boolean
}) {
  const price = resultPrice(item)
  const code = isFlight(item) ? item.destination_city_code : isDestination(item) ? item.code : item.city_code
  const canBook = !isDestination(item)
  const [saved, setSaved] = useState(false)

  return (
    <motion.article className="result-card" initial={{ opacity: 0, y: 18 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.35, delay: index * 0.05 }}>
      <div className="result-image"><img src={imageFor(code)} alt={resultTitle(item, cities)} loading="lazy" /><button className={`favorite-button ${saved ? 'saved' : ''}`} onClick={() => setSaved((value) => !value)} aria-label={saved ? 'Remove from saved places' : 'Save this place'}><Heart size={17} fill={saved ? 'currentColor' : 'none'} /></button>{isHotel(item) && <span className="image-chip"><Star size={12} fill="currentColor" /> {item.rating.toFixed(1)}</span>}{isActivity(item) && <span className="image-chip">LOCAL FAVORITE</span>}</div>
      <div className="result-content"><span className="result-location"><MapPin size={13} /> {resultCity(item, cities)}</span><h3>{resultTitle(item, cities)}</h3><p className="result-meta">{resultMeta(item)}</p><div className="result-footer">{price ? <span className="result-price"><strong>{money(price.amount, price.currency)}</strong><small>{price.suffix}</small></span> : <span className="result-price"><strong>Worth the trip</strong></span>}{canBook && <button className="card-book" onClick={() => onBook(item)} disabled={bookingBusy}>Book <ArrowUpRight size={15} /></button>}</div></div>
    </motion.article>
  )
}

function EmptyResults({ kind, onChangeKind }: { kind: TripKind; onChangeKind: (kind: TripKind) => void }) {
  return <div className="empty-results"><span className="empty-search"><Search size={21} /></span><div><strong>Nothing on the map yet.</strong><p>{kind === 'hotel' ? 'Try another city or adjust your dates and guest count.' : kind === 'flight' ? 'Try a different route or a nearby date.' : 'Try a different search. There’s always another way to get there.'}</p></div><button onClick={() => onChangeKind('destination')}>Explore destinations <ArrowRight size={15} /></button></div>
}

function Modal({ children, onClose }: { children: React.ReactNode; onClose: () => void }) {
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [onClose])

  return <motion.div className="modal-backdrop" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}><motion.div className="modal-card" initial={{ y: 20, scale: 0.97, opacity: 0 }} animate={{ y: 0, scale: 1, opacity: 1 }} exit={{ y: 12, scale: 0.98, opacity: 0 }} transition={{ duration: 0.2 }} role="dialog" aria-modal="true">{children}</motion.div></motion.div>
}

export default App
