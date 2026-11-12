// maximo_region.h: the addresses maximo_boot.cpp uses, for each release of the game.
// maximo_boot.cpp names every game address as it is in the PAL executable (SLES_507.03); GA(pal) gives the same place
// in the release this build is for (the build passes MAXIMO_REGION_USA or MAXIMO_REGION_JPN; none: PAL). The USA and
// Japanese entries were found by matching the three executables function by function (code shape, call targets and
// data references). Only addresses are listed here, no game code or data. A 0 is a place that release does not have
// (debug-only watches); those features are skipped there.
#pragma once
#include <cstdint>

namespace maximo_region
{
    struct Pair
    {
        uint32_t pal, here;
    };
    struct CodeWord
    {
        uint32_t pal, palValue, here; // a word maximo_boot checks before it changes the code: its value in this release
    };

#if defined(MAXIMO_REGION_USA)
    inline constexpr const char *kName = "USA", *kElfName = "SLUS_200.17", *kSerial = "SLUS-20017";
    inline constexpr uint32_t kNativeHz = 60;
    inline constexpr Pair kAddresses[] = {
        {0x100008u, 0x100008u}, // start
        {0x1000C0u, 0x125140u}, // manual
        {0x100450u, 0x1254B0u}, // manual
        {0x101570u, 0x126600u}, // start
        {0x101610u, 0x1266A0u}, // start
        {0x101690u, 0x126720u}, // start
        {0x101820u, 0x1268B0u}, // start
        {0x102120u, 0x1271B0u}, // start
        {0x1028D0u, 0x127960u}, // start
        {0x102B70u, 0x127C00u}, // start
        {0x102D30u, 0x127DC0u}, // start
        {0x102F30u, 0x127FC0u}, // start
        {0x103030u, 0x1280C0u}, // start
        {0x1032A0u, 0x128330u}, // start
        {0x103C30u, 0x128CC0u}, // start
        {0x103CB0u, 0x128D40u}, // start
        {0x104780u, 0x129810u}, // start
        {0x1047C0u, 0x129850u}, // exact
        {0x104850u, 0x1298E0u}, // start
        {0x1049B0u, 0x129A40u}, // start
        {0x104E20u, 0x129EB0u}, // start
        {0x1052B0u, 0x12A340u}, // start
        {0x105370u, 0x12A400u}, // start
        {0x105630u, 0x12A6C0u}, // start
        {0x1056F0u, 0x12A780u}, // start
        {0x10F4B0u, 0x134540u}, // start
        {0x10F530u, 0x1345C0u}, // start
        {0x10F6A0u, 0x134730u}, // start
        {0x112250u, 0x137290u}, // start
        {0x1124D0u, 0x137510u}, // start
        {0x112700u, 0x137740u}, // start
        {0x1163D0u, 0x13B410u}, // start
        {0x117B80u, 0x13CC10u}, // start
        {0x118550u, 0x13D5D0u}, // start
        {0x1186F0u, 0x13D770u}, // start
        {0x1188B0u, 0x13D930u}, // start
        {0x1189F0u, 0x13DA70u}, // start
        {0x118B00u, 0x13DB80u}, // start
        {0x118BB0u, 0x13DC30u}, // start
        {0x118C70u, 0x13DCF0u}, // start
        {0x118D20u, 0x13DDA0u}, // start
        {0x118EF0u, 0x13DF70u}, // start
        {0x119020u, 0x13E0A0u}, // start
        {0x1190B0u, 0x13E130u}, // start
        {0x1191B0u, 0x13E230u}, // start
        {0x1194C0u, 0x13E540u}, // start
        {0x1196B0u, 0x13E740u}, // start
        {0x119A50u, 0x13EAE0u}, // start
        {0x119D20u, 0x13EDB0u}, // start
        {0x119FA0u, 0x13F030u}, // start
        {0x11A380u, 0x13F410u}, // start
        {0x11A4B0u, 0x13F540u}, // start
        {0x11A520u, 0x13F5B0u}, // start
        {0x11B380u, 0x141A70u}, // start
        {0x11BAB0u, 0x1421A0u}, // start
        {0x11F160u, 0x145850u}, // start
        {0x11F740u, 0x145E30u}, // start
        {0x11F7D0u, 0x145EC0u}, // exact
        {0x11F920u, 0x146010u}, // start
        {0x11F938u, 0x146028u}, // exact
        {0x1201A0u, 0x000000u}, // unmapped
        {0x1201E0u, 0x000000u}, // unmapped
        {0x1202C0u, 0x000000u}, // unmapped
        {0x120570u, 0x1468B0u}, // start
        {0x120584u, 0x1468C4u}, // exact
        {0x120590u, 0x1468D0u}, // start
        {0x1205A4u, 0x1468E4u}, // exact
        {0x1205C0u, 0x146900u}, // start
        {0x1205D4u, 0x146914u}, // exact
        {0x1205E0u, 0x146920u}, // start
        {0x12060Cu, 0x14694Cu}, // exact
        {0x120614u, 0x146954u}, // exact
        {0x120640u, 0x146980u}, // start
        {0x120660u, 0x1469A0u}, // exact
        {0x120670u, 0x1469B0u}, // start
        {0x120694u, 0x1469D4u}, // exact
        {0x120730u, 0x146A58u}, // aligned
        {0x12073Cu, 0x146A64u}, // aligned
        {0x120748u, 0x146A70u}, // aligned
        {0x12075Cu, 0x146A84u}, // aligned
        {0x120768u, 0x146A90u}, // aligned
        {0x120770u, 0x146A98u}, // aligned
        {0x120EB0u, 0x1471D0u}, // start
        {0x1212E0u, 0x147600u}, // start
        {0x121510u, 0x147830u}, // start
        {0x1221F0u, 0x148510u}, // start
        {0x128CB0u, 0x150340u}, // start
        {0x1297C0u, 0x150E00u}, // start
        {0x129D40u, 0x151380u}, // start
        {0x12A3F0u, 0x151A30u}, // start
        {0x12CE10u, 0x154430u}, // start
        {0x12E048u, 0x155668u}, // exact
        {0x12E08Cu, 0x1556ACu}, // exact
        {0x1302F0u, 0x157910u}, // start
        {0x1330A4u, 0x15A6C4u}, // exact
        {0x134754u, 0x15BD74u}, // exact
        {0x13BAD0u, 0x1630E0u}, // start
        {0x13C6C0u, 0x163CD0u}, // start
        {0x13CE30u, 0x164440u}, // start
        {0x13D020u, 0x164630u}, // start
        {0x1406C0u, 0x167CD0u}, // start
        {0x145F80u, 0x16D570u}, // start
        {0x147860u, 0x16EE50u}, // start
        {0x147AD0u, 0x16F0D0u}, // start
        {0x147FE0u, 0x16F5E0u}, // start
        {0x14B4F0u, 0x172970u}, // start (frame update)
        {0x13C8B0u, 0x163EC0u}, // start (camera view; orbit camera)
        {0x1483C0u, 0x16F9B0u}, // start (orbit camera)
        {0x153640u, 0x17AAB0u}, // start (line of sight; orbit camera)
        {0x14B7E0u, 0x172C50u}, // start (frame update)
        {0x1480C0u, 0x16F6C0u}, // aligned
        {0x148360u, 0x16F94Cu}, // aligned
        {0x148470u, 0x16FA60u}, // start
        {0x148DC0u, 0x1703B0u}, // start
        {0x148FA0u, 0x170590u}, // start
        {0x1492D0u, 0x1708C0u}, // start
        {0x1493C0u, 0x1709B0u}, // start
        {0x14A610u, 0x171A90u}, // start
        {0x14ABD0u, 0x172050u}, // start
        {0x14AD60u, 0x1721E0u}, // start
        {0x14AEE0u, 0x172360u}, // start
        {0x14AF34u, 0x1723B4u}, // exact
        {0x14AF54u, 0x1723D4u}, // exact
        {0x14B2C0u, 0x172740u}, // start
        {0x14C7D0u, 0x173C40u}, // start
        {0x14DFE0u, 0x175450u}, // start
        {0x14F050u, 0x1764C0u}, // start
        {0x14F2A0u, 0x176710u}, // start
        {0x150140u, 0x1775B0u}, // start
        {0x158260u, 0x17F6D0u}, // start
        {0x15AF20u, 0x182390u}, // start
        {0x15B100u, 0x182570u}, // start
        {0x15BA70u, 0x182EE0u}, // start
        {0x15C8C0u, 0x183D30u}, // start
        {0x15FED0u, 0x187340u}, // start
        {0x15FF00u, 0x187370u}, // start
        {0x15FF30u, 0x1873A0u}, // start
        {0x160960u, 0x187DD0u}, // start
        {0x160B40u, 0x187FB0u}, // start
        {0x160DA0u, 0x188210u}, // start
        {0x160E14u, 0x188284u}, // exact
        {0x160F20u, 0x188390u}, // start
        {0x1610F0u, 0x188560u}, // start
        {0x1611D0u, 0x188640u}, // start
        {0x16722Cu, 0x18E69Cu}, // exact
        {0x167650u, 0x18EAC0u}, // start
        {0x16A598u, 0x191A08u}, // exact
        {0x16A6B0u, 0x191B20u}, // start
        {0x16AE00u, 0x192270u}, // start
        {0x16D450u, 0x1948C0u}, // start
        {0x16D760u, 0x000000u}, // unmapped
        {0x16D9F0u, 0x194DD0u}, // start
        {0x16E120u, 0x195500u}, // start
        {0x170AC0u, 0x19ADB0u}, // start
        {0x17E90Cu, 0x1A8BECu}, // exact
        {0x1811ECu, 0x1AB4CCu}, // exact
        {0x1811F0u, 0x1AB4D0u}, // exact
        {0x1811F4u, 0x1AB4D4u}, // exact
        {0x1811FCu, 0x1AB4DCu}, // exact
        {0x181200u, 0x1AB4E0u}, // exact
        {0x181480u, 0x1AB760u}, // start
        {0x181DC0u, 0x1AC0A0u}, // start
        {0x181DD0u, 0x1AC0B0u}, // start
        {0x1823E0u, 0x1AD300u}, // start
        {0x182810u, 0x1AD730u}, // start
        {0x1831DCu, 0x1AE0FCu}, // exact
        {0x18323Cu, 0x1AE15Cu}, // exact
        {0x190000u, 0x1BAF00u}, // exact
        {0x192FB0u, 0x1BDEB0u}, // start
        {0x197F50u, 0x117F68u}, // start
        {0x197F88u, 0x117FA0u}, // start
        {0x19A540u, 0x11A558u}, // start
        {0x19B100u, 0x11B118u}, // start
        {0x19B2B8u, 0x11B2D0u}, // start
        {0x19C3E8u, 0x11C400u}, // start
        {0x1A08A0u, 0x196398u}, // start
        {0x1A10B0u, 0x196BA8u}, // start
        {0x1A1928u, 0x197420u}, // start
        {0x1A19C8u, 0x1974C0u}, // start
        {0x1A1BA8u, 0x1976A0u}, // start
        {0x1A1E88u, 0x197980u}, // start
        {0x1A2138u, 0x197C30u}, // start
        {0x1A21D0u, 0x197CC8u}, // start
        {0x1A2598u, 0x198090u}, // start
        {0x1A2AF8u, 0x1985F0u}, // start
        {0x1A2B28u, 0x198620u}, // start
        {0x1A2B60u, 0x198658u}, // start
        {0x1A2B90u, 0x198688u}, // start
        {0x1A2BC0u, 0x1986B8u}, // start
        {0x1A2BF8u, 0x1986F0u}, // start
        {0x1A2D80u, 0x198878u}, // start
        {0x1A2DD0u, 0x1988C8u}, // start
        {0x1A2E28u, 0x198920u}, // start
        {0x1A3C68u, 0x102F38u}, // start
        {0x1AA1C0u, 0x100578u}, // start
        {0x1AAA18u, 0x100DD0u}, // start
        {0x1ABAC0u, 0x10C740u}, // start
        {0x1ABE80u, 0x10CB00u}, // start
        {0x1AC320u, 0x10CFA0u}, // start
        {0x1AC32Cu, 0x10CFACu}, // exact
        {0x1AC330u, 0x10CFB0u}, // exact
        {0x1AC334u, 0x10CFB4u}, // exact
        {0x1AC35Cu, 0x10CFDCu}, // exact
        {0x1ACCF0u, 0x10D970u}, // start
        {0x1AE738u, 0x10F3B8u}, // start
        {0x1AEDA8u, 0x10FA28u}, // start
        {0x1AEF78u, 0x10FBF8u}, // start
        {0x1AF168u, 0x10FDE8u}, // start
        {0x1B3460u, 0x1140E0u}, // start
        {0x1B3918u, 0x114598u}, // start
        {0x1B3960u, 0x1145E0u}, // start
        {0x1B6D40u, 0x140408u}, // start
        {0x1B7068u, 0x140730u}, // start
        {0x1B7178u, 0x140840u}, // start
        {0x1B71B0u, 0x140878u}, // start
        {0x1B7268u, 0x140930u}, // start
        {0x1B73D0u, 0x140A98u}, // start
        {0x1B74F0u, 0x140BB8u}, // start
        {0x1B76C0u, 0x140D88u}, // start
        {0x1B77F8u, 0x140EC0u}, // start
        {0x1B7980u, 0x141048u}, // start
        {0x1B7B38u, 0x141200u}, // start
        {0x1B7C68u, 0x141330u}, // start
        {0x1BD870u, 0x1098E0u}, // start
        {0x1BD890u, 0x109900u}, // start
        {0x1BD928u, 0x109998u}, // start
        {0x1BD978u, 0x1099E8u}, // start
        {0x1BE0D8u, 0x10A148u}, // start
        {0x1BE298u, 0x10A308u}, // start
        {0x1BE8C0u, 0x10A930u}, // start
        {0x1BEB40u, 0x10ABB0u}, // start
        {0x1BEC78u, 0x10ACE8u}, // start
        {0x1C0110u, 0x14E590u}, // start
        {0x1C02F0u, 0x14E770u}, // start
        {0x1C0370u, 0x14E7F0u}, // start
        {0x1C0558u, 0x14E9D8u}, // start
        {0x1C06C0u, 0x14EB40u}, // start
        {0x1C0740u, 0x14EBC0u}, // start
        {0x1C07B8u, 0x14EC38u}, // start
        {0x1C07F0u, 0x14EC70u}, // start
        {0x1C08E0u, 0x14ED60u}, // start
        {0x1C0B20u, 0x14EFA0u}, // start
        {0x1C0C58u, 0x14F0D8u}, // start
        {0x1C0D10u, 0x14F190u}, // start
        {0x1C0DC8u, 0x14F248u}, // start
        {0x1C3E80u, 0x1C3740u}, // manual
        {0x1DA030u, 0x1CEF40u}, // data exact
        {0x1E0000u, 0x1D0000u}, // manual
        {0x1E0110u, 0x1D4E40u}, // data exact
        {0x1E25F4u, 0x1D7314u}, // data exact
        {0x1E5A42u, 0x1DA7E2u}, // data exact
        {0x1E70B0u, 0x1C7DC8u}, // data exact
        {0x1E7C60u, 0x1DCA30u}, // data exact
        {0x1E7C80u, 0x1DCA50u}, // data exact
        {0x1E7CA0u, 0x1DCA70u}, // data exact
        {0x1EA400u, 0x1DD600u}, // start
        {0x1EA404u, 0x1DD604u}, // exact
        {0x1EA408u, 0x1DD608u}, // exact
        {0x1EC0F0u, 0x1DF2F0u}, // data exact
        {0x200000u, 0x1F3200u}, // manual
        {0x238ABCu, 0x22BD9Cu}, // data exact
        {0x238ED8u, 0x22C1B8u}, // data exact
        {0x238EDCu, 0x22C1BCu}, // data exact
        {0x238F80u, 0x22C280u}, // data exact
        {0x238FACu, 0x22C2ACu}, // data exact
        {0x238FB4u, 0x22C2B4u}, // data exact
        {0x238FBCu, 0x22C2BCu}, // data exact
        {0x238FC0u, 0x22C2C0u}, // data exact
        {0x238FD4u, 0x22C2D4u}, // data exact
        {0x239030u, 0x22C330u}, // data exact
        {0x239034u, 0x22C334u}, // data exact
        {0x239104u, 0x22C404u}, // data exact
        {0x2393B0u, 0x2312B0u}, // data exact
        {0x2393B4u, 0x2312B4u}, // data bracket
        {0x2393B8u, 0x2312B8u}, // data bracket
        {0x2393BCu, 0x2312BCu}, // data bracket
        {0x2395CCu, 0x2314CCu}, // data bracket
        {0x2395F0u, 0x2314F0u}, // data bracket
        {0x239880u, 0x232DC0u}, // data exact
        {0x239890u, 0x232DD0u}, // data exact
        {0x239A30u, 0x232F70u}, // data exact
        {0x239AD8u, 0x233018u}, // data exact
        {0x25AF14u, 0x254454u}, // data exact
        {0x25B9E0u, 0x254F20u}, // data exact
        {0x25B9E4u, 0x254F24u}, // data exact
        {0x25B9F0u, 0x254F30u}, // data exact
        {0x25B9FCu, 0x254F3Cu}, // data exact
        {0x262524u, 0x25BA64u}, // data bracket
        {0x262528u, 0x25BA68u}, // data bracket
        {0x2625A0u, 0x25BAE0u}, // data exact
        {0x2625F0u, 0x25BB30u}, // data exact
        {0x262704u, 0x25BC44u}, // data exact
        {0x26271Cu, 0x25BC5Cu}, // data bracket
        {0x2633B4u, 0x25C8F4u}, // data exact
        {0x2633B6u, 0x25C8F6u}, // data exact
        {0x2633B8u, 0x25C8F8u}, // data bracket
        {0x2633BCu, 0x25C8FCu}, // data bracket
        {0x2633C0u, 0x25C900u}, // data bracket
        {0x263540u, 0x25CE50u}, // data exact
        {0x26354Au, 0x25CE5Au}, // data exact
        {0x26354Eu, 0x25CE5Eu}, // data exact
        {0x263558u, 0x25CE68u}, // data bracket
        {0x2EC980u, 0x2E4980u}, // data exact
        {0x2F5E00u, 0x2E6280u}, // data exact
    };
    inline constexpr CodeWord kCodeWords[] = {
        {0x120584u, 0x24060180u, 0x24060180u},
        {0x1205A4u, 0x90430003u, 0x90430003u},
        {0x1205D4u, 0xA0460003u, 0xA0460003u},
        {0x12060Cu, 0x24060180u, 0x24060180u},
        {0x120614u, 0x24060480u, 0x24060480u},
        {0x120660u, 0x24060180u, 0x24060180u},
        {0x120694u, 0x28A30180u, 0x28A30180u},
        {0x1047C0u, 0xAE300000u, 0xAE300000u},
        {0x11F7D0u, 0x24060604u, 0x24060604u},
        {0x11F938u, 0x240600C0u, 0x240600C0u},
        {0x1811ECu, 0x8F83876Cu, 0x8F83864Cu},
        {0x1811F0u, 0x8F828768u, 0x8F828648u},
        {0x1811F4u, 0x3C050200u, 0x3C050200u},
        {0x1811FCu, 0x00A31823u, 0x00A31823u},
        {0x181200u, 0x00621023u, 0x00621023u},
        {0x1EA404u, 0x08060474u, 0x0806AD2Cu},
        {0x1EA408u, 0x244493B0u, 0x244412B0u},
        {0x1EC0F0u, 0x001EA400u, 0x001DD600u},
        {0x120730u, 0x24447CA0u, 0x2444CA70u},
        {0x12073Cu, 0x0C0484B8u, 0x0C051D80u},
        {0x120748u, 0x24447C60u, 0x2444CA30u},
        {0x12075Cu, 0x0C0484B8u, 0x0C051D80u},
        {0x120768u, 0x24447C80u, 0x2444CA50u},
        {0x120770u, 0x0C048544u, 0x0C051E0Cu},
    };
#elif defined(MAXIMO_REGION_JPN)
    inline constexpr const char *kName = "Japan", *kElfName = "SLPM_621.27", *kSerial = "SLPM-62127";
    inline constexpr uint32_t kNativeHz = 60;
    inline constexpr Pair kAddresses[] = {
        {0x100008u, 0x100008u}, // start
        {0x1000C0u, 0x125140u}, // manual
        {0x100450u, 0x1254B0u}, // manual
        {0x101570u, 0x126600u}, // start
        {0x101610u, 0x1266A0u}, // start
        {0x101690u, 0x126720u}, // start
        {0x101820u, 0x1268B0u}, // start
        {0x102120u, 0x1271B0u}, // start
        {0x1028D0u, 0x127960u}, // start
        {0x102B70u, 0x127C00u}, // start
        {0x102D30u, 0x127DC0u}, // start
        {0x102F30u, 0x127FC0u}, // start
        {0x103030u, 0x1280C0u}, // start
        {0x1032A0u, 0x128330u}, // start
        {0x103C30u, 0x128CC0u}, // start
        {0x103CB0u, 0x128D40u}, // start
        {0x104780u, 0x129810u}, // start
        {0x1047C0u, 0x129850u}, // exact
        {0x104850u, 0x1298E0u}, // start
        {0x1049B0u, 0x129A40u}, // start
        {0x104E20u, 0x129EB0u}, // start
        {0x1052B0u, 0x12A340u}, // start
        {0x105370u, 0x12A400u}, // start
        {0x105630u, 0x12A6C0u}, // start
        {0x1056F0u, 0x12A780u}, // start
        {0x10F4B0u, 0x134540u}, // start
        {0x10F530u, 0x1345C0u}, // start
        {0x10F6A0u, 0x134730u}, // start
        {0x112250u, 0x137290u}, // start
        {0x1124D0u, 0x137510u}, // start
        {0x112700u, 0x137740u}, // start
        {0x1163D0u, 0x13B410u}, // start
        {0x117B80u, 0x13CC10u}, // start
        {0x118550u, 0x13D5A0u}, // start
        {0x1186F0u, 0x13D740u}, // start
        {0x1188B0u, 0x13D900u}, // start
        {0x1189F0u, 0x13DA40u}, // start
        {0x118B00u, 0x13DB50u}, // start
        {0x118BB0u, 0x13DC00u}, // start
        {0x118C70u, 0x13DCC0u}, // start
        {0x118D20u, 0x13DD70u}, // start
        {0x118EF0u, 0x13DF40u}, // start
        {0x119020u, 0x13E070u}, // start
        {0x1190B0u, 0x13E100u}, // start
        {0x1191B0u, 0x13E200u}, // start
        {0x1194C0u, 0x13E510u}, // start
        {0x1196B0u, 0x13E710u}, // start
        {0x119A50u, 0x13EAB0u}, // start
        {0x119D20u, 0x13ED80u}, // start
        {0x119FA0u, 0x13F000u}, // start
        {0x11A380u, 0x13F3E0u}, // start
        {0x11A4B0u, 0x13F510u}, // start
        {0x11A520u, 0x13F580u}, // start
        {0x11B380u, 0x000000u}, // unmapped
        {0x11BAB0u, 0x1421B0u}, // start
        {0x11F160u, 0x145950u}, // start
        {0x11F740u, 0x145F30u}, // start
        {0x11F7D0u, 0x145FC0u}, // exact
        {0x11F920u, 0x146110u}, // start
        {0x11F938u, 0x146128u}, // exact
        {0x1201A0u, 0x000000u}, // unmapped
        {0x1201E0u, 0x000000u}, // unmapped
        {0x1202C0u, 0x000000u}, // unmapped
        {0x120570u, 0x1469B0u}, // start
        {0x120584u, 0x1469C4u}, // exact
        {0x120590u, 0x1469D0u}, // start
        {0x1205A4u, 0x1469E4u}, // exact
        {0x1205C0u, 0x146A00u}, // start
        {0x1205D4u, 0x146A14u}, // exact
        {0x1205E0u, 0x146A20u}, // start
        {0x12060Cu, 0x146A4Cu}, // exact
        {0x120614u, 0x146A54u}, // exact
        {0x120640u, 0x146A80u}, // start
        {0x120660u, 0x146AA0u}, // exact
        {0x120670u, 0x146AB0u}, // start
        {0x120694u, 0x146AD4u}, // exact
        {0x120730u, 0x146B58u}, // aligned
        {0x12073Cu, 0x146B64u}, // aligned
        {0x120748u, 0x146B70u}, // aligned
        {0x12075Cu, 0x146B84u}, // aligned
        {0x120768u, 0x146B90u}, // aligned
        {0x120770u, 0x146B98u}, // aligned
        {0x120EB0u, 0x1472D0u}, // start
        {0x1212E0u, 0x147700u}, // start
        {0x121510u, 0x147930u}, // start
        {0x1221F0u, 0x148610u}, // start
        {0x128CB0u, 0x150560u}, // start
        {0x1297C0u, 0x151020u}, // start
        {0x129D40u, 0x1515A0u}, // start
        {0x12A3F0u, 0x151C50u}, // start
        {0x12CE10u, 0x154650u}, // start
        {0x12E048u, 0x155888u}, // exact
        {0x12E08Cu, 0x1558CCu}, // exact
        {0x1302F0u, 0x157B20u}, // start
        {0x1330A4u, 0x15A8A4u}, // aligned
        {0x134754u, 0x15BF34u}, // exact
        {0x13BAD0u, 0x163270u}, // start
        {0x13C6C0u, 0x163E60u}, // start
        {0x13CE30u, 0x1645D0u}, // start
        {0x13D020u, 0x1647C0u}, // start
        {0x1406C0u, 0x167E60u}, // start
        {0x145F80u, 0x16D630u}, // start
        {0x147860u, 0x16EF10u}, // start
        {0x147AD0u, 0x16F190u}, // start
        {0x147FE0u, 0x16F6A0u}, // start
        {0x14B4F0u, 0x172CB0u}, // start (frame update)
        {0x13C8B0u, 0x164050u}, // start (camera view; orbit camera)
        {0x1483C0u, 0x16FA70u}, // start (orbit camera)
        {0x153640u, 0x17ADB0u}, // start (line of sight; orbit camera)
        {0x14B7E0u, 0x172F90u}, // start (frame update)
        {0x1480C0u, 0x16F780u}, // aligned
        {0x148360u, 0x16FA0Cu}, // aligned
        {0x148470u, 0x16FB20u}, // start
        {0x148DC0u, 0x170470u}, // start
        {0x148FA0u, 0x170650u}, // start
        {0x1492D0u, 0x170980u}, // start
        {0x1493C0u, 0x170A70u}, // start
        {0x14A610u, 0x000000u}, // unmapped
        {0x14ABD0u, 0x172390u}, // start
        {0x14AD60u, 0x172520u}, // start
        {0x14AEE0u, 0x1726A0u}, // start
        {0x14AF34u, 0x1726F4u}, // exact
        {0x14AF54u, 0x172714u}, // exact
        {0x14B2C0u, 0x172A80u}, // start
        {0x14C7D0u, 0x173F80u}, // start
        {0x14DFE0u, 0x175790u}, // start
        {0x14F050u, 0x176800u}, // start
        {0x14F2A0u, 0x176A50u}, // start
        {0x150140u, 0x1778E0u}, // start
        {0x158260u, 0x17F9D0u}, // start
        {0x15AF20u, 0x182690u}, // start
        {0x15B100u, 0x182870u}, // start
        {0x15BA70u, 0x1831E0u}, // start
        {0x15C8C0u, 0x184030u}, // start
        {0x15FED0u, 0x187640u}, // start
        {0x15FF00u, 0x187670u}, // start
        {0x15FF30u, 0x1876A0u}, // start
        {0x160960u, 0x1880D0u}, // start
        {0x160B40u, 0x1882B0u}, // start
        {0x160DA0u, 0x188510u}, // start
        {0x160E14u, 0x188584u}, // exact
        {0x160F20u, 0x188690u}, // start
        {0x1610F0u, 0x188860u}, // start
        {0x1611D0u, 0x188940u}, // start
        {0x16722Cu, 0x18E99Cu}, // exact
        {0x167650u, 0x18EDC0u}, // start
        {0x16A598u, 0x191D08u}, // exact
        {0x16A6B0u, 0x191E20u}, // start
        {0x16AE00u, 0x192570u}, // start
        {0x16D450u, 0x194BC0u}, // start
        {0x16D760u, 0x000000u}, // unmapped
        {0x16D9F0u, 0x1950C0u}, // start
        {0x16E120u, 0x1957F0u}, // start
        {0x170AC0u, 0x19B0A0u}, // start
        {0x17E90Cu, 0x1A8EDCu}, // exact
        {0x1811ECu, 0x1AB7BCu}, // exact
        {0x1811F0u, 0x1AB7C0u}, // exact
        {0x1811F4u, 0x1AB7C4u}, // exact
        {0x1811FCu, 0x1AB7CCu}, // exact
        {0x181200u, 0x1AB7D0u}, // exact
        {0x181480u, 0x1ABA50u}, // start
        {0x181DC0u, 0x1AC390u}, // start
        {0x181DD0u, 0x1AC3A0u}, // start
        {0x1823E0u, 0x1AD5F0u}, // start
        {0x182810u, 0x1ADA20u}, // start
        {0x1831DCu, 0x1AE3ECu}, // exact
        {0x18323Cu, 0x1AE44Cu}, // exact
        {0x190000u, 0x1BB1F0u}, // exact
        {0x192FB0u, 0x1BE1A0u}, // start
        {0x197F50u, 0x117F68u}, // start
        {0x197F88u, 0x117FA0u}, // start
        {0x19A540u, 0x11A558u}, // start
        {0x19B100u, 0x11B118u}, // start
        {0x19B2B8u, 0x11B2D0u}, // start
        {0x19C3E8u, 0x11C400u}, // start
        {0x1A08A0u, 0x196688u}, // start
        {0x1A10B0u, 0x196E98u}, // start
        {0x1A1928u, 0x197710u}, // start
        {0x1A19C8u, 0x1977B0u}, // start
        {0x1A1BA8u, 0x197990u}, // start
        {0x1A1E88u, 0x197C70u}, // start
        {0x1A2138u, 0x197F20u}, // start
        {0x1A21D0u, 0x197FB8u}, // start
        {0x1A2598u, 0x198380u}, // start
        {0x1A2AF8u, 0x1988E0u}, // start
        {0x1A2B28u, 0x198910u}, // start
        {0x1A2B60u, 0x198948u}, // start
        {0x1A2B90u, 0x198978u}, // start
        {0x1A2BC0u, 0x1989A8u}, // start
        {0x1A2BF8u, 0x1989E0u}, // start
        {0x1A2D80u, 0x198B68u}, // start
        {0x1A2DD0u, 0x198BB8u}, // start
        {0x1A2E28u, 0x198C10u}, // start
        {0x1A3C68u, 0x102F38u}, // start
        {0x1AA1C0u, 0x100578u}, // start
        {0x1AAA18u, 0x100DD0u}, // start
        {0x1ABAC0u, 0x10C740u}, // start
        {0x1ABE80u, 0x10CB00u}, // start
        {0x1AC320u, 0x10CFA0u}, // start
        {0x1AC32Cu, 0x10CFACu}, // exact
        {0x1AC330u, 0x10CFB0u}, // exact
        {0x1AC334u, 0x10CFB4u}, // exact
        {0x1AC35Cu, 0x10CFDCu}, // exact
        {0x1ACCF0u, 0x10D970u}, // start
        {0x1AE738u, 0x10F3B8u}, // start
        {0x1AEDA8u, 0x10FA28u}, // start
        {0x1AEF78u, 0x10FBF8u}, // start
        {0x1AF168u, 0x10FDE8u}, // start
        {0x1B3460u, 0x1140E0u}, // start
        {0x1B3918u, 0x114598u}, // start
        {0x1B3960u, 0x1145E0u}, // start
        {0x1B6D40u, 0x1403D8u}, // start
        {0x1B7068u, 0x140700u}, // start
        {0x1B7178u, 0x140810u}, // start
        {0x1B71B0u, 0x140848u}, // start
        {0x1B7268u, 0x140900u}, // start
        {0x1B73D0u, 0x140A68u}, // start
        {0x1B74F0u, 0x140B88u}, // start
        {0x1B76C0u, 0x140D58u}, // start
        {0x1B77F8u, 0x140E90u}, // start
        {0x1B7980u, 0x141018u}, // start
        {0x1B7B38u, 0x1411D0u}, // start
        {0x1B7C68u, 0x141300u}, // start
        {0x1BD870u, 0x1098E0u}, // start
        {0x1BD890u, 0x109900u}, // start
        {0x1BD928u, 0x109998u}, // start
        {0x1BD978u, 0x1099E8u}, // start
        {0x1BE0D8u, 0x10A148u}, // start
        {0x1BE298u, 0x10A308u}, // start
        {0x1BE8C0u, 0x10A930u}, // start
        {0x1BEB40u, 0x10ABB0u}, // start
        {0x1BEC78u, 0x10ACE8u}, // start
        {0x1C0110u, 0x14E7B0u}, // start
        {0x1C02F0u, 0x14E990u}, // start
        {0x1C0370u, 0x14EA10u}, // start
        {0x1C0558u, 0x14EBF8u}, // start
        {0x1C06C0u, 0x14ED60u}, // start
        {0x1C0740u, 0x14EDE0u}, // start
        {0x1C07B8u, 0x14EE58u}, // start
        {0x1C07F0u, 0x14EE90u}, // start
        {0x1C08E0u, 0x14EF80u}, // start
        {0x1C0B20u, 0x14F1C0u}, // start
        {0x1C0C58u, 0x14F2F8u}, // start
        {0x1C0D10u, 0x14F3B0u}, // start
        {0x1C0DC8u, 0x14F468u}, // start
        {0x1C3E80u, 0x1C3A30u}, // manual
        {0x1DA030u, 0x1D1920u}, // data exact
        {0x1E0000u, 0x1D0000u}, // manual
        {0x1E0110u, 0x1D7720u}, // data exact
        {0x1E25F4u, 0x1DECC4u}, // data exact
        {0x1E5A42u, 0x1E2192u}, // data exact
        {0x1E70B0u, 0x1C80C8u}, // data exact
        {0x1E7C60u, 0x1E43B0u}, // data exact
        {0x1E7C80u, 0x1E43D0u}, // data exact
        {0x1E7CA0u, 0x1E43F0u}, // data exact
        {0x1EA400u, 0x1E4F80u}, // start
        {0x1EA404u, 0x1E4F84u}, // exact
        {0x1EA408u, 0x1E4F88u}, // exact
        {0x1EC0F0u, 0x1E6C70u}, // data exact
        {0x200000u, 0x1FAB80u}, // manual
        {0x238ABCu, 0x2332BCu}, // data exact
        {0x238ED8u, 0x233700u}, // data exact
        {0x238EDCu, 0x233704u}, // data exact
        {0x238F80u, 0x233780u}, // data exact
        {0x238FACu, 0x2337ACu}, // data exact
        {0x238FB4u, 0x2337B4u}, // data exact
        {0x238FBCu, 0x2337BCu}, // data exact
        {0x238FC0u, 0x2337C0u}, // data exact
        {0x238FD4u, 0x2337D4u}, // data exact
        {0x239030u, 0x233828u}, // data exact
        {0x239034u, 0x23382Cu}, // data exact
        {0x239104u, 0x233904u}, // data exact
        {0x2393B0u, 0x2387B0u}, // data exact
        {0x2393B4u, 0x2387B4u}, // data bracket
        {0x2393B8u, 0x2387B8u}, // data bracket
        {0x2393BCu, 0x2387BCu}, // data bracket
        {0x2395CCu, 0x2389CCu}, // data bracket
        {0x2395F0u, 0x2389F0u}, // data bracket
        {0x239880u, 0x23A2C0u}, // data exact
        {0x239890u, 0x23A2D0u}, // data exact
        {0x239A30u, 0x23A470u}, // data exact
        {0x239AD8u, 0x23A518u}, // data exact
        {0x25AF14u, 0x25B954u}, // data exact
        {0x25B9E0u, 0x25C420u}, // data exact
        {0x25B9E4u, 0x25C424u}, // data exact
        {0x25B9F0u, 0x25C430u}, // data exact
        {0x25B9FCu, 0x25C43Cu}, // data exact
        {0x262524u, 0x262F64u}, // data bracket
        {0x262528u, 0x262F68u}, // data bracket
        {0x2625A0u, 0x262FE0u}, // data exact
        {0x2625F0u, 0x263030u}, // data exact
        {0x262704u, 0x263144u}, // data exact
        {0x26271Cu, 0x26315Cu}, // data bracket
        {0x2633B4u, 0x263DF4u}, // data exact
        {0x2633B6u, 0x263DF6u}, // data exact
        {0x2633B8u, 0x263DF8u}, // data bracket
        {0x2633BCu, 0x263DFCu}, // data bracket
        {0x2633C0u, 0x263E00u}, // data bracket
        {0x263540u, 0x264350u}, // data exact
        {0x26354Au, 0x26435Au}, // data exact
        {0x26354Eu, 0x26435Eu}, // data exact
        {0x263558u, 0x264368u}, // data bracket
        {0x2EC980u, 0x2EBE40u}, // data exact
        {0x2F5E00u, 0x2ED700u}, // data exact
    };
    inline constexpr CodeWord kCodeWords[] = {
        {0x120584u, 0x24060180u, 0x24060180u},
        {0x1205A4u, 0x90430003u, 0x90430003u},
        {0x1205D4u, 0xA0460003u, 0xA0460003u},
        {0x12060Cu, 0x24060180u, 0x24060180u},
        {0x120614u, 0x24060480u, 0x24060480u},
        {0x120660u, 0x24060180u, 0x24060180u},
        {0x120694u, 0x28A30180u, 0x28A30180u},
        {0x1047C0u, 0xAE300000u, 0xAE300000u},
        {0x11F7D0u, 0x24060604u, 0x24060604u},
        {0x11F938u, 0x240600C0u, 0x240600C0u},
        {0x1811ECu, 0x8F83876Cu, 0x8F838694u},
        {0x1811F0u, 0x8F828768u, 0x8F828690u},
        {0x1811F4u, 0x3C050200u, 0x3C050200u},
        {0x1811FCu, 0x00A31823u, 0x00A31823u},
        {0x181200u, 0x00621023u, 0x00621023u},
        {0x1EA404u, 0x08060474u, 0x0806ADE8u},
        {0x1EA408u, 0x244493B0u, 0x244487B0u},
        {0x1EC0F0u, 0x001EA400u, 0x001E4F80u},
        {0x120730u, 0x24447CA0u, 0x244443F0u},
        {0x12073Cu, 0x0C0484B8u, 0x0C051DC0u},
        {0x120748u, 0x24447C60u, 0x244443B0u},
        {0x12075Cu, 0x0C0484B8u, 0x0C051DC0u},
        {0x120768u, 0x24447C80u, 0x244443D0u},
        {0x120770u, 0x0C048544u, 0x0C051E4Cu},
    };
#else
    inline constexpr const char *kName = "PAL", *kElfName = "SLES_507.03", *kSerial = "SLES-50703";
    inline constexpr uint32_t kNativeHz = 50;
#define MAXIMO_REGION_PAL 1
#endif
    inline constexpr bool kPal = kNativeHz == 50;

    // The address of PAL address pal in this release (evaluated while compiling; an address missing from the table
    // stops the build, so none can be forgotten).
    consteval uint32_t GA(uint32_t pal)
    {
#if defined(MAXIMO_REGION_PAL)
        return pal;
#else
        for (const Pair &p : kAddresses)
            if (p.pal == pal)
                return p.here;
        throw "maximo_region::GA: this address has no entry for this release";
#endif
    }

    // The value a checked code word has in this release.
    consteval uint32_t GW(uint32_t pal, uint32_t palValue)
    {
#if defined(MAXIMO_REGION_PAL)
        return (void)pal, palValue;
#else
        for (const CodeWord &w : kCodeWords)
            if (w.pal == pal && w.palValue == palValue)
                return w.here;
        return palValue;
#endif
    }
}
