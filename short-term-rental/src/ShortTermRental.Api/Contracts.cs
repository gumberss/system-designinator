namespace ShortTermRental.Api;

public sealed record BookingRequest(Guid PropertyId, Guid GuestId, DateOnly CheckIn, DateOnly CheckOut);
public sealed record Booking(Guid Id, Guid PropertyId, Guid GuestId, DateOnly CheckIn,
    DateOnly CheckOut, string Status, DateTime CreatedAt);
public sealed record Property(Guid Id, string Name, string City);
public sealed record Availability(Guid PropertyId, DateOnly CheckIn, DateOnly CheckOut, bool Available);
public sealed record BookingEvent(Guid EventId, string EventType, Guid BookingId, Guid PropertyId);
public sealed record BookingResult(Booking Booking, bool Replayed);

public static class BookingRules
{
    public static string? ValidateDates(DateOnly checkIn, DateOnly checkOut)
    {
        if (checkIn == default || checkOut == default)
            return "Informe checkIn e checkOut.";
        if (checkOut <= checkIn)
            return "checkOut deve ser posterior a checkIn.";
        if (checkOut.DayNumber - checkIn.DayNumber > 365)
            return "Uma estadia pode ter no maximo 365 noites.";
        return null;
    }

    public static string? Validate(BookingRequest request)
    {
        if (request.PropertyId == Guid.Empty || request.GuestId == Guid.Empty)
            return "propertyId e guestId devem ser UUIDs nao vazios.";
        return ValidateDates(request.CheckIn, request.CheckOut);
    }
}

public sealed class BookingConflictException(string message) : Exception(message);
public sealed class ResourceNotFoundException(string message) : Exception(message);
