//////////////////////////////////////////////////////////////
// Tarkov Bright — шейдер «чтобы всё было видно»
//
// Делает то же, что Python-приложение, но прямо в игре и вручную:
//   * поднимает тени гаммой, а не «яркостью всего кадра»
//   * держит контраст: чёрная точка + подъём деталей, поэтому нет «мыла»
//   * сажает пересветы (фонарь, NVG, снег) filmic-коленом
//   * нейтрализует сине-зелёный тилт выбранного цвета
//   * убирает виньетку Таркова (её гаммой не вытянуть)
//   * локальный контраст только по теням + дизер против полос
//
// Установка: скопировать в <папка игры>\reshade-shaders\Shaders\
// Нужен ReShade 4.9+/5 и пакет "Universal / API" (там ReShade.fxh).
//
// ВАЖНО: это НЕ авто-режим. Авто-подстройку под каждую сцену делает
// приложение (app/main.py); шейдер — это стабильный ручной вариант.
//////////////////////////////////////////////////////////////

#include "ReShade.fxh"

// --- локальные UI-макросы: не зависим от версии Macros.fxh ---------------
#define TB_SLIDER(var, label, tooltip, minv, maxv, defv) \
    uniform float var < ui_type = "slider"; ui_label = label; ui_tooltip = tooltip; \
                        ui_min = minv; ui_max = maxv; > = defv;
#define TB_BOOL(var, label, tooltip, defv) \
    uniform bool var < ui_type = "bool"; ui_label = label; ui_tooltip = tooltip; > = defv;
#define TB_COLOR(var, label, tooltip, r, g, b) \
    uniform float3 var < ui_type = "color"; ui_label = label; ui_tooltip = tooltip; \
                         ui_color = { r, g, b }; > = float3(r, g, b);

//--------------------------------------------------------------
// Параметры. Дефолты = «ночь/лес». Для Labs убавьте Gamma до ~1.15,
// TintAmount до 0.5, Vignette до 0.2.
//--------------------------------------------------------------
TB_BOOL(TB_Enable, "Включить", "Экстренный выключатель, если стало хуже", true)

TB_SLIDER(TB_Gamma, "Гамма (подъём теней)", "1.0 = ничего; ночь 1.4-1.6; Labs/день 1.1-1.25; >2.0 пойдёт мыло", 1.0, 2.4, 1.45)
TB_SLIDER(TB_ShadowDetail, "Детали в тенях", "Локальная гамма только внизу: держит разброс теней, свет не трогает", 0.0, 1.0, 0.50)
TB_SLIDER(TB_BlackPoint, "Чёрная точка", "Съедает мёртвые пиксели виньетки. Ночь 0.4-0.6, помещение 0.1-0.2", 0.0, 1.0, 0.45)
TB_SLIDER(TB_ShadowLift, "Подъём чёрных", "Как Black Level монитора. 0 = контрастнее", 0.0, 1.0, 0.10)
TB_SLIDER(TB_Highlight, "Защита пересветов", "Фонарь/NVG/снег не должны выжигать кадр", 0.0, 1.0, 0.30)

TB_COLOR(TB_TintCast, "Цвет тилта для нейтрализации", "Поставьте пипеткой цвет «зелени/синевы» Таркова", 0.62, 0.78, 0.95)
TB_SLIDER(TB_TintAmount, "Сила нейтрализации тилта", "0 = не трогать, 0.5 = заметно, >0.8 = риск розового", 0.0, 1.0, 0.35)

TB_SLIDER(TB_Saturation, "Насыщенность", "1.1-1.25 обычно за глаза", 0.8, 1.6, 1.15)
TB_SLIDER(TB_Vignette, "Убрать виньетку", "Насколько осветлить края кадра", 0.0, 1.0, 0.55)
TB_SLIDER(TB_Clarity, "Локальный контраст (тени)", "Различение силуэтов. >0.5 полезет зерно", 0.0, 1.0, 0.25)
TB_SLIDER(TB_ClarityRadius, "Радиус локального контраста", "В пикселях: 2-4 для 1080p, 4-8 для 4K", 1.0, 12.0, 3.0)
TB_SLIDER(TB_Dither, "Дизер против полос", "0.5-1.0 убирает бандинг на поднятых тенях", 0.0, 1.0, 0.65)
TB_BOOL(TB_ShowMask, "Показать маску виньетки", "Диагностика: светлее = сильнее подтягивает край", false)

#define TB_G (2.2)

float3 tb_to_linear(float3 c) { return pow(max(c, 0.0), TB_G); }
float3 tb_to_srgb(float3 c) { return pow(max(c, 0.0), 1.0 / TB_G); }
float tb_luma(float3 c) { return dot(c, float3(0.2126, 0.7152, 0.0722)); }

// Нейтрализация выбранного «налётного» цвета. Ровно та же математика,
// что auto_tint() в Python: gain = mean/channels, нормировка по
// геометрическому среднему (яркость не уезжает), лимит, затем сила.
float3 tb_tint_gains()
{
	float3 cast = max(TB_TintCast, 0.05);
	float avg = (cast.r + cast.g + cast.b) / 3.0;
	float3 g = avg / cast;
	g = clamp(g, 1.0 / 1.25, 1.25);
	float geo = pow(max(g.r * g.g * g.b, 1e-6), 1.0 / 3.0);
	g = clamp(g / geo, 1.0 / 1.25, 1.25);
	return 1.0 + (g - 1.0) * TB_TintAmount;
}

float3 tb_grade(float3 srgb)
{
	float3 lin = tb_to_linear(srgb);

	// 1) чёрная точка: всё, что ниже «дна» кадра, в ноль + нормировка.
	//    0.006 — не случайно: 0.3*0.3*0.006 = ровно тот уровень, который
	//    авто-алгоритм приложения берёт из p05 типичной ночной сцены.
	if (TB_BlackPoint > 0.0)
	{
		float bp = TB_BlackPoint * TB_BlackPoint * 0.006;
		lin = saturate((lin - bp) / (1.0 - bp));
	}

	float3 x = tb_to_srgb(lin);

	// 2) мягкий подъём абсолютной черноты (как Black Level монитора)
	if (TB_ShadowLift > 0.0)
		x = saturate(x + TB_ShadowLift * 0.02 * pow(1.0 - x, 2.0));

	// 3) гамма + «детали в тенях» + тилт — одной степенью по каналам.
	//    Множитель (1 + D*(1-x)^2) даёт сильную гамму в чёрном и 1.0 в белом,
	//    поэтому разброс теней РАСТЯГИВАЕТСЯ, а не схлопывается в молоко.
	float3 expo = TB_Gamma * (1.0 + TB_ShadowDetail * pow(1.0 - saturate(x), 2.0))
	                    * pow(max(tb_tint_gains(), 0.05), 1.3);
	x = pow(saturate(x), 1.0 / expo);

	// 4) filmic-колено: приглушаем клампинг, белый остаётся белым
	if (TB_Highlight > 0.0)
	{
		float k = TB_Highlight * 0.6;
		x = saturate(x) * (1.0 + k) / (1.0 + k * saturate(x));
	}

	// 5) насыщенность вокруг яркости в линейном свете
	if (abs(TB_Saturation - 1.0) > 0.001)
	{
		float3 l2 = tb_to_linear(saturate(x));
		float l = tb_luma(l2);
		x = tb_to_srgb(saturate(l + (l2 - l) * TB_Saturation));
	}

	return saturate(x);
}

// Виньетка Таркова: подтягиваем края ровно там, где они затемнены.
float tb_vignette_factor(float2 uv)
{
	float2 d = (uv - 0.5) * 2.0;
	float r = saturate(dot(d, d) * 0.7);
	return 1.0 + TB_Vignette * 0.55 * r;
}

float3 tb_hash(float2 p)
{
	float n = sin(dot(p, float2(12.9898, 78.233))) * 43758.5453;
	return frac(float3(n, n * 1.7, n * 3.1));
}

float4 PS_TarkovBright(float4 pos : SV_Position, float2 uv : TEXCOORD) : SV_Target
{
	if (TB_ShowMask)
		return float4(tb_vignette_factor(uv) - 1.0, 1.0, 1.0, 1.0);

	if (!TB_Enable)
		return float4(tex2D(ReShade::BackBuffer, uv).rgb, 1.0);

	float2 px = TB_ClarityRadius * BUFFER_PIXEL_SIZE;

	float3 c0 = tex2D(ReShade::BackBuffer, uv).rgb;

	// лёгкий подъём до градовки: виньетку снимаем в исходном кадре,
	// иначе она удваивается гаммой
	float3 lifted = saturate(c0 * tb_vignette_factor(uv));

	float3 g = tb_grade(lifted);

	// дизер до финального клиппинга — иначе ступеньки в тенях не убрать
	if (TB_Dither > 0.0)
		g += (tb_hash(pos.xy) - 0.5) * (TB_Dither / 255.0);

	// локальный контраст только в тёмных областях (на светлых он усилит зерно)
	if (TB_Clarity > 0.0)
	{
		float3 blur = lifted;
		blur += tex2D(ReShade::BackBuffer, uv + float2(px.x, 0.0)).rgb;
		blur += tex2D(ReShade::BackBuffer, uv - float2(px.x, 0.0)).rgb;
		blur += tex2D(ReShade::BackBuffer, uv + float2(0.0, px.y)).rgb;
		blur += tex2D(ReShade::BackBuffer, uv - float2(0.0, px.y)).rgb;
		blur *= 0.2;
		float detail = tb_luma(g) - tb_luma(tb_grade(blur));
		float shadow_weight = saturate(1.0 - tb_luma(g) * 2.2);
		g += detail * TB_Clarity * 1.6 * shadow_weight;
	}

	return float4(saturate(g), 1.0);
}

technique TarkovBrightVisibility
{
	pass Main
	{
		VertexShader = PostProcessVS;
		PixelShader = PS_TarkovBright;
	}
}
