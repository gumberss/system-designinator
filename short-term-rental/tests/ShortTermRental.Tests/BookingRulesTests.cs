using ShortTermRental.Api;

namespace ShortTermRental.Tests;

public class BookingRulesTests
{
    private static readonly DateOnly Start = new(2030, 1, 1);

    [Theory]
    [InlineData(1)]
    [InlineData(30)]
    [InlineData(365)]
    public void ValidStayIsAccepted(int nights) =>
        Assert.Null(BookingRules.ValidateDates(Start, Start.AddDays(nights)));

    [Theory]
    [InlineData(0)]
    [InlineData(-1)]
    [InlineData(366)]
    public void InvalidLengthIsRejected(int nights) =>
        Assert.NotNull(BookingRules.ValidateDates(Start, Start.AddDays(nights)));

    [Fact]
    public void MissingDatesAreRejected()
    {
        Assert.NotNull(BookingRules.ValidateDates(default, Start));
        Assert.NotNull(BookingRules.ValidateDates(Start, default));
    }

    [Fact]
    public void EmptyIdentifiersAreRejected()
    {
        Assert.NotNull(BookingRules.Validate(new(Guid.Empty, Guid.NewGuid(), Start, Start.AddDays(1))));
        Assert.NotNull(BookingRules.Validate(new(Guid.NewGuid(), Guid.Empty, Start, Start.AddDays(1))));
    }

    [Fact]
    public void HistoricalDatesRemainValidForIdempotentRetries() =>
        Assert.Null(BookingRules.Validate(new(Guid.NewGuid(), Guid.NewGuid(),
            new DateOnly(2020, 1, 1), new DateOnly(2020, 1, 2))));
}
