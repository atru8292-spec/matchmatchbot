-- Блок 13 (доводка перед деплоем):
--  * ссылка на курсы вынесена в плейсхолдер [course_link] (app_settings) вместо мёртвого
--    www.rusaenmexico.com; приглашение на курс — ОТДЕЛЬНЫМ бабблом, чтобы при пустой ссылке
--    отправитель убрал только его, не теряя остальной текст;
--  * сценарий 47 (день ивента): время 8:30 PM → плейсхолдер [hora] (из /set_event);
--  * сценарии 47/50: строка [event_link] (ссылка оплаты/брони, из /set_event_link).
-- SET-идемпотентно (повторный прогон безопасен).

-- Сценарий 10: приглашение на курсы + «приоритет прошедшим курсы» — В ОДНОМ баббле,
-- чтобы при пустом [course_link] оба упоминания курсов убрались вместе (иначе висит
-- «сначала берём прошедших курсы» без объяснения, что за курсы).
UPDATE scenarios SET template_es = E'Gracias por el info guapo 🤍\n\nMira, ahorita tengo una lista de espera que puede tardar entre 6 y 12 meses.\n\nMientras, te invito a tomar nuestros cursos en línea aquí: [course_link]. Cuando se desocupan lugares para la agencia, primero consideramos a quienes ya tomaron los cursos, y de ahí los demás'
WHERE id = 10;

UPDATE scenarios SET template_es = E'Gracias por la foto guapo 🤍\n\nAhorita tengo una lista de espera para la agencia.\n\nMientras, te invito a tomar nuestro curso de cómo conectar con mujeres eslavas aquí: [course_link]'
WHERE id = 12;

UPDATE scenarios SET template_es = E'Todo bien guapo, respeto tu decisión 🤍\n\nTe dejo en nuestra lista por si algún día cambia algo.\n\nMientras, te invito a tomar nuestros cursos en línea aquí: [course_link] ✨'
WHERE id = 17;

UPDATE scenarios SET template_es = E'Te agradezco la honestidad guapo 🤍\n\nPero nosotros trabajamos solo con personas que buscan relación seria. Las chicas en nuestra base buscan matrimonio, familia, pareja a largo plazo\n\nSi cambias tu enfoque, aquí estaremos ✨\n\nMientras, te invito a tomar nuestros cursos en línea aquí: [course_link]'
WHERE id = 27;

UPDATE scenarios SET template_es = E'Hola guapo! 🤍 hoy es el día del evento, qué emoción! Te espero a las [hora]\n\nTe paso la ubicación y los detalles aquí: [dirección, lugar, parking]. Cualquier cosa me avisas ✨\n\n💳 Reserva tu lugar aquí: [event_link]'
WHERE id = 47;

UPDATE scenarios SET template_es = E'Hola guapo! 🤍 te recuerdo que mañana es el evento ✨ Te espero a las [hora] en [dirección]. Cualquier duda me avisas 🤍\n\n💳 Reserva tu lugar aquí: [event_link]'
WHERE id = 50;

-- Сценарий 47 имеет embedding — без trigger_type='scheduled' он попадёт в RAG для входящих
-- и уйдёт лиду с сырыми [hora]/[dirección] (планировщик его тогда не наполняет). Закрываем.
UPDATE scenarios SET trigger_type = 'scheduled' WHERE id = 47;
