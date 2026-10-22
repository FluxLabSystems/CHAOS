namespace Chaos.Host.Maintenance;

/// <summary>
/// When the next scheduled run is, as a pure function of the clock, the
/// configured time of day and a time zone.
/// </summary>
internal static class MaintenanceSchedule
{
    /// <summary>
    /// The next instant at which <paramref name="timeOfDay"/> occurs in
    /// <paramref name="zone"/>, strictly after <paramref name="nowUtc"/>.
    /// </summary>
    /// <param name="nowUtc">The current instant.</param>
    /// <param name="timeOfDay">A wall-clock time of day, at least zero and less than 24 hours.</param>
    /// <param name="zone">The zone the time of day is read in.</param>
    /// <returns>The next run, as a UTC instant.</returns>
    /// <remarks>
    /// A wall-clock time inside a spring-forward gap never happens; the run is
    /// moved to the same minute after the jump (02:30 becomes 03:30). An
    /// ambiguous wall-clock time at fall-back is taken at its standard-time
    /// occurrence, the second one. Either way the run happens once per day.
    /// </remarks>
    public static DateTimeOffset NextRun(DateTimeOffset nowUtc, TimeSpan timeOfDay, TimeZoneInfo zone)
    {
        ArgumentNullException.ThrowIfNull(zone);
        if (timeOfDay < TimeSpan.Zero || timeOfDay >= TimeSpan.FromDays(1))
        {
            throw new ArgumentOutOfRangeException(nameof(timeOfDay), timeOfDay, "A time of day is at least 00:00 and less than 24:00.");
        }

        var localNow = TimeZoneInfo.ConvertTime(nowUtc, zone);
        var today = localNow.Date;

        // Today, tomorrow, and one more in case a gap or an offset change pushed
        // tomorrow's candidate back before now; three is always enough.
        for (var days = 0; days < 3; days++)
        {
            var wall = DateTime.SpecifyKind(today.AddDays(days) + timeOfDay, DateTimeKind.Unspecified);
            var candidate = ToInstant(wall, zone);
            if (candidate > nowUtc)
            {
                return candidate.ToUniversalTime();
            }
        }

        throw new InvalidOperationException(
            $"No occurrence of {timeOfDay} in {zone.Id} was found after {nowUtc:O}; this cannot happen.");
    }

    private static DateTimeOffset ToInstant(DateTime wall, TimeZoneInfo zone)
    {
        if (zone.IsInvalidTime(wall))
        {
            wall = wall.AddHours(1);
        }

        return new DateTimeOffset(wall, zone.GetUtcOffset(wall));
    }
}
