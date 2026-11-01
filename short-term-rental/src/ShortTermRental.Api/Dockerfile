FROM mcr.microsoft.com/dotnet/sdk:10.0 AS build
WORKDIR /source
COPY global.json Directory.Build.props NuGet.Config ./
COPY src/ShortTermRental.Api/ShortTermRental.Api.csproj src/ShortTermRental.Api/
RUN dotnet restore src/ShortTermRental.Api/ShortTermRental.Api.csproj
COPY src/ShortTermRental.Api/ src/ShortTermRental.Api/
RUN dotnet publish src/ShortTermRental.Api/ShortTermRental.Api.csproj -c Release -o /app --no-restore

FROM mcr.microsoft.com/dotnet/aspnet:10.0
WORKDIR /app
COPY --from=build /app .
USER $APP_UID
ENTRYPOINT ["dotnet", "ShortTermRental.Api.dll"]
