// Thin wrapper that exports ONE coin face of the zCloud coin master as flat 2D
// geometry, so FreeCAD can import it as SVG and V-carve it.
//
//   openscad -o anverso-5.svg -D 'vista="none"' -D 'face="anverso"' \
//            -D 'diametro=37' -D 'valor="5"' face2d.scad
//
// vista="none" suppresses the master's own top-level output; only the 2D face
// module below is rendered.
include </home/edimar/Projetos/edimar/my-ideas/eletronica/moeda-zcloud-master.scad>

face = "anverso";

if (face == "anverso") anverso_2d(diametro, valor); else reverso_2d(diametro);
