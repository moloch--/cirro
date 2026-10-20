# Read-only on-prem AD input for tools/test_graphqlite_ad.py.
# Run on a domain controller or a domain-joined host with the ActiveDirectory
# module and directory read access. Writes JSON to stdout; no directory changes.
# Explicit field lists exclude credentials, descriptions and arbitrary attributes.
$ErrorActionPreference = 'Stop'
Import-Module ActiveDirectory

function Get-SidValue($Object) {
    if ($null -ne $Object.SID) { return [string]$Object.SID.Value }
    if ($null -ne $Object.ObjectSid) { return [string]$Object.ObjectSid.Value }
    return $null
}

$domain = Get-ADDomain
$domainObject = Get-ADObject -Identity $domain.DistinguishedName -Properties objectSid
$users = @(Get-ADUser -Filter * -Properties memberOf, primaryGroupID | Sort-Object DistinguishedName | ForEach-Object {
    [ordered]@{
        objectGUID = [string]$_.ObjectGUID
        objectSid = Get-SidValue $_
        distinguishedName = [string]$_.DistinguishedName
        name = [string]$_.Name
        samAccountName = [string]$_.SamAccountName
        userPrincipalName = [string]$_.UserPrincipalName
        enabled = [bool]$_.Enabled
        memberOf = @($_.MemberOf | Sort-Object)
        primaryGroupID = [int]$_.PrimaryGroupID
    }
})
$groups = @(Get-ADGroup -Filter * -Properties members, memberOf | Sort-Object DistinguishedName | ForEach-Object {
    [ordered]@{
        objectGUID = [string]$_.ObjectGUID
        objectSid = Get-SidValue $_
        distinguishedName = [string]$_.DistinguishedName
        name = [string]$_.Name
        samAccountName = [string]$_.SamAccountName
        groupCategory = [string]$_.GroupCategory
        groupScope = [string]$_.GroupScope
        members = @($_.Members | Sort-Object)
        memberOf = @($_.MemberOf | Sort-Object)
    }
})
$computers = @(Get-ADComputer -Filter * -Properties memberOf, primaryGroupID, operatingSystem | Sort-Object DistinguishedName | ForEach-Object {
    [ordered]@{
        objectGUID = [string]$_.ObjectGUID
        objectSid = Get-SidValue $_
        distinguishedName = [string]$_.DistinguishedName
        name = [string]$_.Name
        samAccountName = [string]$_.SamAccountName
        dnsHostName = [string]$_.DNSHostName
        enabled = [bool]$_.Enabled
        operatingSystem = [string]$_.OperatingSystem
        memberOf = @($_.MemberOf | Sort-Object)
        primaryGroupID = [int]$_.PrimaryGroupID
    }
})
$ous = @(Get-ADOrganizationalUnit -Filter * | Sort-Object DistinguishedName | ForEach-Object {
    [ordered]@{
        objectGUID = [string]$_.ObjectGUID
        distinguishedName = [string]$_.DistinguishedName
        name = [string]$_.Name
    }
})
$trusts = @(Get-ADTrust -Filter * | Sort-Object Target | ForEach-Object {
    [ordered]@{
        source = [string]$_.Source
        target = [string]$_.Target
        direction = [string]$_.Direction
        trustType = [string]$_.TrustType
        intraForest = [bool]$_.IntraForest
    }
})
[ordered]@{
    schema_version = 1
    source = [ordered]@{
        kind = 'on-prem-active-directory'
        hostname = $env:COMPUTERNAME
        collectedAt = [DateTime]::UtcNow.ToString('o')
        collector = 'Cirro read-only AD validation snapshot'
    }
    domain = [ordered]@{
        name = [string]$domain.Name
        dnsRoot = [string]$domain.DNSRoot
        distinguishedName = [string]$domain.DistinguishedName
        objectGUID = [string]$domainObject.ObjectGUID
        objectSid = Get-SidValue $domainObject
    }
    users = $users
    groups = $groups
    computers = $computers
    organizationalUnits = $ous
    trusts = $trusts
} | ConvertTo-Json -Depth 8 -Compress
