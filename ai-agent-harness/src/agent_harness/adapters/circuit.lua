local key = KEYS[1]
local action, generation, token = ARGV[1], tonumber(ARGV[2]), ARGV[3]
local probe, threshold = tonumber(ARGV[4]), tonumber(ARGV[5])
local recovery, probe_timeout, cooldown = tonumber(ARGV[6]), tonumber(ARGV[7]), tonumber(ARGV[8])
local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000
local state = redis.call('HMGET', key, 'generation', 'failures', 'opened_until', 'probe_until', 'token')
local gen, failures = tonumber(state[1]) or 0, tonumber(state[2]) or 0
local opened, until_probe, owner = tonumber(state[3]) or 0, tonumber(state[4]) or 0, state[5] or ''
if action == 'acquire' then
    if opened > now or until_probe > now then return {0, gen, 0} end
    probe = 0
    if opened ~= 0 then
        probe, gen, owner, until_probe = 1, gen + 1, token, now + probe_timeout
    end
elseif gen ~= generation or (probe == 1 and (owner ~= token or now >= until_probe)) then
    return {0, gen, 0}
elseif action == 'success' then
    failures, opened, until_probe, owner = 0, 0, 0, ''
elseif action == 'failure' then
    failures = failures + 1
    if probe == 1 or cooldown >= 0 or failures >= threshold then
        local duration = recovery
        if cooldown >= 0 then duration = cooldown end
        gen, opened, until_probe, owner = gen + 1, now + duration, 0, ''
    end
elseif action == 'neutral' and probe == 1 then
    gen, opened, until_probe, owner = gen + 1, now + recovery, 0, ''
end
redis.call('HSET', key, 'generation', gen, 'failures', failures, 'opened_until', opened,
    'probe_until', until_probe, 'token', owner)
redis.call('EXPIRE', key, math.ceil(math.min(2147483647,
    math.max(86400, 4 * (recovery + probe_timeout + math.max(0, cooldown))))))
return {1, gen, probe}
